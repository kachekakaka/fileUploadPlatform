from contextlib import contextmanager
import json
import ntpath
import os
from pathlib import Path
import stat
from uuid import UUID

import yaml

from upload_platform.domain.core import Problem, uid


class UniqueSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise Problem("invalid_manifest", "YAML 键必须是唯一字符串")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_yaml(text):
    try:
        value = yaml.load(text, Loader=UniqueSafeLoader)
        if not isinstance(value, dict):
            raise ValueError("manifest 必须为对象")
        return value
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise Problem("invalid_manifest", "描述文件格式不正确或含不支持的 YAML tag") from exc


def lexical_root(value):
    # Pure normalization is used for idempotency BEFORE any filesystem access.
    if not isinstance(value, str) or not value or "\x00" in value:
        raise Problem("invalid_path", "目录路径无效")
    if value.startswith(("\\\\", "//")):
        raise Problem("invalid_path", "不允许 UNC 或设备路径")
    drive, rest = ntpath.splitdrive(value)
    if ":" in rest or (drive and not rest.startswith(("/", "\\"))):
        raise Problem("invalid_path", "不允许 ADS 或盘符相对路径")
    if not Path(value).is_absolute():
        raise Problem("invalid_path", "作品目录必须为本机绝对路径")
    return os.path.normcase(os.path.normpath(value))


def root_path(value):
    path = Path(lexical_root(str(value)))
    try:
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise OSError()
        lexical_root(str(path))
        return path
    except (OSError, RuntimeError) as exc:
        raise Problem("invalid_path", "作品目录不存在或不可访问") from exc


def relative_path(value):
    path = value.replace("\\", "/")
    if not path or path.startswith("/") or ":" in path or "\x00" in path:
        raise Problem("invalid_path", "素材必须是安全相对路径")
    if any(p in ("..", "") for p in path.split("/")):
        raise Problem("invalid_path", "不允许路径穿越")
    if any(p.rstrip(" .") != p or p == "." for p in path.split("/")):
        raise Problem("invalid_path", "不允许含歧义的路径段")
    return path


@contextmanager
def open_asset(directory, relative, *, text=False):
    root = root_path(directory)
    rel = relative_path(relative)
    candidate = root / rel
    handle = None
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise OSError()
        handle = open(candidate, "r" if text else "rb", **({"encoding": "utf-8"} if text else {}))
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError()
        if os.name == "nt":
            import msvcrt
            import win32file

            final = win32file.GetFinalPathNameByHandle(msvcrt.get_osfhandle(handle.fileno()), 0)
            if final.startswith("\\\\?\\UNC\\"):
                raise OSError()
            final = Path(final.removeprefix("\\\\?\\")).resolve()
        else:
            final = candidate.resolve(strict=True)
        if not final.is_relative_to(root):
            raise OSError()
    except (OSError, RuntimeError) as exc:
        if handle:
            handle.close()
        raise Problem(
            "invalid_path",
            "文件不存在、不可读、非普通文件或已越出作品目录",
            details={"relative_path": rel},
        ) from exc
    try:
        yield handle
    finally:
        handle.close()


def asset_info(directory, relative):
    with open_asset(directory, relative) as handle:
        info = os.fstat(handle.fileno())
        return {
            "relative_path": relative_path(relative),
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
        }


def read_manifest(directory):
    with open_asset(directory, "upload.yaml", text=True) as handle:
        return parse_yaml(handle.read())


def atomic_write(path, text):
    """Only remove the uniquely created temporary file owned by this operation."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uid() + ".tmp")
    try:
        with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            import win32file

            win32file.MoveFileEx(str(temporary), str(path), 0x1 | 0x8)
        else:
            os.replace(temporary, path)
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))


def prepare(directory, *, ready=False):
    directory = root_path(directory)
    path = directory / "upload.yaml"
    lock = directory / ".upload-platform-manifest.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise Problem(
            "manifest_locked", "描述文件有独占锁；确认原操作结束后再处理该锁", 409
        ) from exc
    try:
        if path.exists():
            value = read_manifest(str(directory))
        else:
            value = {
                "version": 1,
                "work": {"ready": False, "title": "待编辑作品"},
                "assets": {"videos": []},
                "upload_units": [],
            }
        work = value.setdefault("work", {})
        if not isinstance(work, dict):
            raise Problem("invalid_manifest", "work 必须为对象")
        if "id" in work:
            try:
                UUID(work["id"])
            except (ValueError, TypeError, AttributeError) as exc:
                raise Problem("invalid_manifest", "已有 work.id 非法，不自动替换") from exc
        else:
            work["id"] = uid()
        if ready:
            from upload_platform.application.manifest import validate_manifest

            work["ready"] = True
            validate_manifest(str(directory), value)
        atomic_write(path, yaml.safe_dump(value, allow_unicode=True, sort_keys=False))
        return value
    finally:
        os.close(fd)
        lock.unlink()
