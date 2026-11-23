from collections import OrderedDict
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal

from pydantic import Field, UUID4, field_validator

from upload_platform.application.contracts import Manifest, StrictModel, Submission
from upload_platform.domain.core import Problem, uid, utc
from upload_platform.infrastructure.files import lexical_root, open_asset, parse_yaml, root_path


SCOPE = "POST:/submissions"
YAML_LIMIT = 1024 * 1024
PREVIEW_TTL = 600
PREVIEW_LIMIT = 256
Version = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
LocalPath = Annotated[str, Field(min_length=1, max_length=32767)]
SourceLabel = Annotated[str, Field(min_length=1, max_length=80)]


class SubmissionPaths(StrictModel):
    manifest_path: LocalPath
    asset_root: LocalPath

    @field_validator("manifest_path", "asset_root")
    @classmethod
    def normalize(cls, value):
        return lexical_root(value)


class SubmissionSource(StrictModel):
    application: SourceLabel
    kind: SourceLabel | None = None
    external_reference: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class Immediate(StrictModel):
    type: Literal["immediate"] = "immediate"


class AutomaticSubmission(SubmissionPaths):
    expected_version: Version
    execution: Immediate = Field(default_factory=Immediate)
    source: SubmissionSource | None = None


class ManualSubmission(StrictModel):
    preview_id: Annotated[UUID4, Field(strict=False)]
    execution: Immediate = Field(default_factory=Immediate)
    source: SubmissionSource | None = None


class SubmissionPreview(StrictModel):
    preview_id: str
    work_id: str
    config_version: Version
    expires_at: str
    manifest: Manifest
    warnings: list[str]


class SubmissionReceipt(StrictModel):
    work_id: str
    task_ids: list[str]
    status: Literal["accepted"]
    config_version: Version


class WorkRegistration(StrictModel):
    work_id: str
    config_version: Version


@dataclass(frozen=True)
class Preview:
    paths: SubmissionPaths
    version: str
    expires: float


def version(content):
    return blake2b(content, digest_size=32).hexdigest()


def local_root(value):
    directory = root_path(value)
    if directory.drive:
        import win32file

        if win32file.GetDriveType(str(directory.anchor)) == 4:  # DRIVE_REMOTE
            raise Problem("invalid_path", "不允许网络驱动器")
    return directory


def read_submission_manifest(paths, expected=None):
    path = Path(paths.manifest_path)
    if path.name != "upload.yaml":
        raise Problem("invalid_path", "配置文件名必须是 upload.yaml")
    manifest_directory = local_root(path.parent)
    asset_root = local_root(paths.asset_root)
    with open_asset(manifest_directory, path.name, fixed_root=True) as handle:
        content = handle.read(YAML_LIMIT + 1)
    if len(content) > YAML_LIMIT:
        raise Problem("invalid_manifest", "配置超过 1 MiB")
    revision = version(content)
    if expected is not None and revision != expected:
        raise Problem("version_conflict", "配置版本已变化，请重新预览或更新预期版本", 409)
    try:
        manifest = parse_yaml(content.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise Problem("invalid_manifest", "配置必须使用 UTF-8 编码") from exc
    return manifest, str(asset_root), revision


class UnifiedSubmissions:
    def __init__(self, service):
        self.service = service
        self.lock = Lock()
        self.previews = OrderedDict()

    def prepare(self, paths, expected=None):
        manifest, root, revision = read_submission_manifest(paths, expected)
        prepared = self.service.prepare_work(root, manifest, fixed_root=True)
        if not prepared["expanded"]:
            raise Problem("no_targets", "没有启用的上传目标")
        return {
            **prepared,
            "manifest_path": paths.manifest_path,
            "config_version": revision,
        }

    def preview(self, paths):
        prepared = self.prepare(paths)
        identifier = uid()
        revision = prepared["config_version"]
        with self.lock:
            expires = self.service.clock() + PREVIEW_TTL
            self.previews[identifier] = Preview(paths, revision, expires)
            if len(self.previews) > PREVIEW_LIMIT:
                self.previews.popitem(last=False)
        return {
            "preview_id": identifier,
            "work_id": prepared["manifest"]["work"]["id"],
            "config_version": revision,
            "manifest": self.service.public_manifest(prepared["manifest"]),
            "warnings": [],
            "expires_at": utc(expires),
        }

    def manual(self, request):
        with self.lock:
            preview = self.previews.get(str(request.preview_id))
        if preview is None or preview.expires <= self.service.clock():
            raise Problem("preview_expired", "预览已失效，请重新预览", 409)
        return self.prepare(preview.paths, preview.version)

    def register(self, key, request):
        def prepare():
            value = self.manual(request)
            value["source"] = (
                request.source.model_dump() if request.source else {"application": "local_import"}
            )
            return value

        def action(conn, prepared):
            self.service.save_work(conn, prepared)
            return {
                "work_id": prepared["manifest"]["work"]["id"],
                "config_version": prepared["config_version"],
            }

        return self.service.mutate("POST:/dashboard/works", key, request, prepare, action, 201)

    def submit(self, key, request):
        def prepare():
            if isinstance(request, ManualSubmission):
                value = self.manual(request)
            else:
                value = self.prepare(request, request.expected_version)
            value["source"] = request.source.model_dump() if request.source else None
            return value

        def action(conn, prepared):
            saved = self.service.submit_prepared(conn, prepared, Submission())
            return {
                "work_id": saved["work_id"],
                "task_ids": [task["task_id"] for task in saved["tasks"]],
                "status": "accepted",
                "config_version": prepared["config_version"],
            }

        return self.service.mutate(SCOPE, key, request, prepare, action, 201)

    def result(self, key):
        with self.service.database.read() as conn:
            record = self.service.receipt_record(conn, SCOPE, key)
        if record is None:
            raise Problem("submission_not_found", "没有找到该请求的受理回执", 404)
        return record["response"]
