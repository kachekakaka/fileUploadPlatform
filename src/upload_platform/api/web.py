from collections import deque
from pathlib import Path
import re
from secrets import compare_digest, token_urlsafe
from time import monotonic
from uuid import UUID

from fastapi import Header
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from upload_platform.application.contracts import Empty
from upload_platform.application.submissions import (
    AutomaticSubmission,
    ManualSubmission,
    SubmissionPaths,
    SubmissionPreview,
    SubmissionReceipt,
    WorkRegistration,
    local_root,
)
from upload_platform.domain.core import Problem
from upload_platform.infrastructure.directory_picker import DirectoryPicker


PREFIX = "/api/v1"
SUBMIT = PREFIX + "/submissions"
PREVIEW = SUBMIT + "/preview"
RESULT = SUBMIT + "/result"
PUBLIC_METHODS = {SUBMIT: "POST", PREVIEW: "POST", RESULT: "GET"}
DASHBOARD_SESSION = PREFIX + "/dashboard/session"
DEPLOYMENT_HEADER = "X-Upload-Platform-Deployment"
MUMU_ORIGIN = "http://127.0.0.1:8000"
WRITES = {"POST", "PATCH", "PUT", "DELETE"}
IDENTIFIER = r"[0-9a-fA-F-]{36}"
PAGE = re.compile(
    rf"/(?:|overview|accounts|settings|schedules|(?:embed/)?(?:works|tasks)(?:/{IDENTIFIER})?|embed/accounts)"
)
READ = re.compile(
    rf"{PREFIX}/(?:dashboard/session|system/(?:health|overview|capabilities)|platforms"
    rf"|works(?:/{IDENTIFIER}(?:/tasks)?)?|tasks(?:/{IDENTIFIER}(?:/(?:attempts|queries|"
    rf"resolutions|events(?:/stream)?))?)?|schedules(?:/{IDENTIFIER}(?:/runs)?)?"
    rf"|schedule-runs/{IDENTIFIER}|account-login-sessions(?:/{IDENTIFIER}(?:/qr)?)?)"
)
WRITE = re.compile(
    rf"{PREFIX}/(?:dashboard/(?:pick-directory|works)|account-login-sessions(?:/{IDENTIFIER}/(?:cancel|resume))?"
    rf"|accounts/{IDENTIFIER}|tasks/{IDENTIFIER}/(?:cancel|resubmit|query|resolve)"
    rf"|schedules(?:/{IDENTIFIER})?)"
)


def key(request, value):
    try:
        identifier = UUID(value)
        if identifier.version == 4 and len(request.headers.getlist("idempotency-key")) == 1:
            return str(identifier)
    except (ValueError, TypeError, AttributeError):
        pass
    raise Problem("invalid_key", "此接口需要随机 UUIDv4 Idempotency-Key", 400)


def cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Expose-Headers"] = DEPLOYMENT_HEADER
    response.headers["Cache-Control"] = "no-store"
    return response


def management_view(request, data):
    if not getattr(request.state, "dashboard", False):
        return data

    def redact(value):
        if isinstance(value, dict):
            return {
                key: redact(item)
                for key, item in value.items()
                if key not in {"credential_reference", "old_credential_reference"}
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    return redact(data)


def preflight(request):
    headers = request.headers
    requested = {
        name.strip().lower()
        for name in headers.get("access-control-request-headers", "").split(",")
        if name.strip()
    }
    method = PUBLIC_METHODS[request.url.path]
    allowed = {"content-type", "idempotency-key", DEPLOYMENT_HEADER.lower()}
    if (
        not headers.get("origin")
        or headers.get("access-control-request-method") != method
        or requested - allowed
    ):
        raise Problem("invalid_preflight", "预检的方法或请求头不受支持", 400)
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Methods": method,
            "Access-Control-Allow-Headers": f"Content-Type, Idempotency-Key, {DEPLOYMENT_HEADER}",
            "Vary": "Access-Control-Request-Method, Access-Control-Request-Headers",
        },
    )


class DashboardAccess:
    def __init__(self, config):
        self.origin = config.api_url.rstrip("/")
        self.csrf = token_urlsafe(32)
        self.writes = deque()
        self.polls = deque()

    @staticmethod
    def limit(history, maximum):
        at = monotonic()
        while history and history[0] <= at - 60:
            history.popleft()
        if len(history) >= maximum:
            raise Problem("rate_limited", "操作过于频繁，请稍后重试", 429)
        history.append(at)

    def authorize(self, request):
        headers, method = request.headers, request.method
        path = request.url.path
        origins = headers.getlist("origin")
        if headers.get("sec-fetch-site") != "same-origin" or (origins and origins != [self.origin]):
            raise Problem("forbidden_source", "管理接口须由面板自身页面访问", 403)
        if method in WRITES:
            tokens = headers.getlist("x-upload-platform-csrf")
            if (
                not WRITE.fullmatch(path)
                or origins != [self.origin]
                or len(tokens) != 1
                or not tokens[0].isascii()
                or not compare_digest(self.csrf, tokens[0])
            ):
                raise Problem("forbidden_source", "管理操作的来源或防跨站凭据无效", 403)
            self.limit(self.writes, 90)
            return
        if path == PREFIX + "/accounts" and request.query_params.get("view") == "summary":
            return
        if method != "GET" or not READ.fullmatch(path):
            raise Problem("forbidden_source", "此入口未向面板开放", 403)
        if path.startswith(PREFIX + "/account-login-sessions"):
            self.limit(self.polls, 180)


def attach_web(app, config, dashboard, submissions):
    from fastapi import Request

    picker = DirectoryPicker()

    @app.get(DASHBOARD_SESSION, include_in_schema=False)
    def session():
        return {"csrf_token": dashboard.csrf}

    @app.post(PREFIX + "/dashboard/pick-directory", include_in_schema=False)
    def pick_directory(body: Empty, request: Request):
        if not request.state.dashboard:
            raise Problem("forbidden_source", "目录选择必须从面板发起", 403)
        files = submissions.service.manifest_files
        common = next(
            (
                child
                for config_root in files.manifest_roots
                for asset_root in files.asset_roots
                for child, ancestor in ((config_root, asset_root), (asset_root, config_root))
                if child.is_relative_to(ancestor)
            ),
            None,
        )
        if common is None:
            raise Problem("picker_unavailable", "配置目录和素材目录分开时，请分别输入路径", 409)
        selected = picker.choose(common)
        if selected is None:
            return {"directory": None}
        directory = local_root(selected)
        files.within(directory, files.manifest_roots)
        files.within(directory, files.asset_roots)
        return {"directory": str(directory)}

    @app.post(
        PREFIX + "/dashboard/works",
        response_model=WorkRegistration,
        status_code=201,
        include_in_schema=False,
    )
    def register(
        body: ManualSubmission,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ):
        if not request.state.dashboard:
            raise Problem("forbidden_source", "此登记入口只接收面板请求", 403)
        _, value = submissions.register(key(request, idempotency_key), body)
        return value

    @app.post(PREVIEW, response_model=SubmissionPreview, response_model_exclude_unset=True)
    def preview(body: SubmissionPaths):
        return submissions.preview(body)

    @app.post(SUBMIT, response_model=SubmissionReceipt, status_code=201)
    def submit(
        body: AutomaticSubmission | ManualSubmission,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ):
        _, receipt = submissions.submit(key(request, idempotency_key), body)
        return receipt

    @app.get(RESULT, response_model=SubmissionReceipt)
    def result(request: Request, idempotency_key: str = Header(alias="Idempotency-Key")):
        return submissions.result(key(request, idempotency_key))

    directory = config.dashboard_dir or Path(__file__).resolve().parents[3] / "web" / "dist"
    app.mount(
        "/assets", StaticFiles(directory=directory / "assets", check_dir=False), name="assets"
    )

    @app.get("/{page:path}", include_in_schema=False)
    def dashboard_page(page: str):
        if not PAGE.fullmatch("/" + page):
            raise Problem("not_found", "页面不存在", 404)
        index = directory / "index.html"
        if not index.is_file():
            raise Problem("dashboard_unavailable", "Dashboard 静态文件尚未构建", 503)
        return FileResponse(
            index,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    f"frame-ancestors {MUMU_ORIGIN}; default-src 'self'; "
                    "object-src 'none'; base-uri 'none'; form-action 'self'; "
                    "style-src 'self' 'unsafe-inline'; img-src 'self' data:"
                ),
                "Referrer-Policy": "no-referrer",
            },
        )
