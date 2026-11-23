"""Process and storage management for the Windows portable distribution."""

import argparse
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
from threading import Event, Lock, Thread
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
from uuid import UUID, uuid4

from upload_platform.config import Config
from upload_platform.domain.core import Problem
from upload_platform.infrastructure.database import SCHEMA_VERSION, Database
from upload_platform.infrastructure.files import lexical_root
from upload_platform.infrastructure.windows import process_creation

ADDRESS = "http://127.0.0.1:8765"
ROLES = ("server", "worker")
ENGINE = "bilibili-upload-engine.exe"
BUILD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


def fail(message):
    raise Problem("portable_startup", message)


def load_json(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def save_json(path, value):
    temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def within_either(first, second):
    return first.is_relative_to(second) or second.is_relative_to(first)


def stable_directory(value, *, exists):
    path = Path(lexical_root(str(value)))
    if path.resolve() != path or (exists and not path.is_dir()):
        fail(f"目录不存在或指向其他位置\n{path}")
    return path


def engine_capabilities(directory, build_id):
    if not BUILD_ID.fullmatch(build_id) or build_id.lower().endswith("-fixture"):
        fail("便携版需要正式 B 站引擎构建号")
    try:
        expected = load_json(directory / "capabilities.json")
        executable = directory / ENGINE
        with executable.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise ValueError("Expected a Windows executable")
            stream.seek(0x3C)
            stream.seek(int.from_bytes(stream.read(4), "little"))
            if stream.read(6) != b"PE\0\0\x64\x86":
                raise ValueError("Expected an x64 engine")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        actual = json.loads(
            subprocess.check_output(
                [str(executable), "capabilities"],
                cwd=directory,
                timeout=10,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                encoding="utf-8",
            )
        )
        if (
            not isinstance(actual, dict)
            or actual != expected
            or actual.get("engine_build_id") != build_id
            or type(actual.get("protocol_version")) is not int
            or actual["protocol_version"] != 1
            or actual.get("platform_verification") != "verified"
            or actual.get("submit_api") != "web"
            or actual.get("upload_ready") is not True
            or actual.get("supports_self_only") is not True
            or not {"login", "upload", "query"}.issubset(actual["commands"])
            or actual["login_interaction"]
            != {
                "version": 1,
                "modes": ["terminal", "machine"],
                "image_type": "image/png",
            }
            or actual["metadata_limits"]["part_count"] < 3
        ):
            raise ValueError("Incompatible capability output")
        return actual
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise Problem("portable_engine", f"B 站引擎缺失、无法运行或能力不符\n{directory}") from exc


@contextmanager
def exclusive(path):
    import msvcrt

    with path.open("a+b") as stream:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise Problem("portable_busy", "正在启动或退出，请稍后再查看") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def live_process(record):
    import pywintypes
    import win32api
    import win32event

    try:
        process = win32api.OpenProcess(0x00100000 | 0x1000, False, record["pid"])
    except pywintypes.error as exc:
        if exc.winerror in (87, 1168):
            return False
        raise Problem("portable_process", "无法核验后台进程身份，请检查运行权限") from exc
    try:
        return (
            win32event.WaitForSingleObject(process, 0) == win32event.WAIT_TIMEOUT
            and process_creation(process) == record["creation_time"]
        )
    finally:
        process.Close()


class Portable:
    def __init__(self, bundle, home):
        self.bundle = bundle.resolve(strict=True)
        self.home = stable_directory(home, exists=False)
        self.settings_file = self.home / "settings.json"
        self.opener = build_opener(ProxyHandler({}))

    @classmethod
    def for_user(cls, bundle):
        return cls(bundle, Path(os.environ["LOCALAPPDATA"]) / "fileUploadPlatform" / "MuMu")

    @property
    def default_data(self):
        return self.home / "data"

    def settings(self):
        if not self.settings_file.exists():
            return None
        try:
            data = load_json(self.settings_file)
            required = {"data", "deployment_id"}
            if set(data) not in (required, required | {"recordings"}):
                raise ValueError()
            if not isinstance(data["data"], str):
                raise ValueError()
            if data["deployment_id"] is not None and not is_deployment_id(data["deployment_id"]):
                raise ValueError()
            return data
        except (OSError, ValueError) as exc:
            raise Problem("portable_settings", f"无法读取便携版设置\n{self.settings_file}") from exc

    def data_directory(self, value):
        data = stable_directory(value, exists=False)
        if within_either(data, self.bundle) or self.home.is_relative_to(data):
            fail("程序、启动设置和私有数据须使用分开的目录")
        stable_directory(data / "core", exists=False)
        stable_directory(data / "bilibili-private", exists=False)
        stable_directory(data / "logs", exists=False)
        return data

    def configure(self):
        data = self.data_directory(self.default_data)
        if data.exists() and (not data.is_dir() or any(data.iterdir())):
            fail(f"默认数据目录已有内容，无法确认原部署，请核对便携版设置和原数据库\n{data}")
        data.mkdir(exist_ok=True, parents=True)
        settings = {"data": str(data), "deployment_id": None}
        save_json(self.settings_file, settings)
        return settings

    def bundle_engine(self):
        try:
            manifest = load_json(self.bundle / "release.json")
            build_id = manifest["engine_build_id"]
            if not isinstance(build_id, str) or not BUILD_ID.fullmatch(build_id):
                raise ValueError()
        except (OSError, ValueError, KeyError) as exc:
            raise Problem("portable_release", "便携包版本清单缺失或损坏，请完整解压") from exc
        engine = self.bundle / "engines" / build_id
        engine_capabilities(engine, build_id)
        return build_id, engine

    @contextmanager
    def operation(self):
        self.home.mkdir(parents=True, exist_ok=True)
        with exclusive(self.home / "launcher.lock"):
            yield

    def record_path(self, role):
        return self.home / f"{role}.json"

    def record(self, role, deployment):
        path = self.record_path(role)
        if not path.exists():
            return None
        value = load_json(path)
        if type(value["pid"]) is not int or value["pid"] <= 0:
            fail(f"后台进程记录无效\n{path}")
        if not live_process(value):
            return None
        if value["deployment_id"] != deployment or value["role"] != role:
            fail(f"后台 {role} 正在使用其他部署")
        if not re.fullmatch(r"[0-9a-f]{32}", value["token"]):
            fail(f"后台进程记录无效\n{path}")
        return value

    def marker(self, token):
        return self.home / f"{token}.stop"

    def diagnostics(self, route):
        with self.opener.open(ADDRESS + "/api/v1/" + route, timeout=2) as response:
            return json.load(response)

    def health(self, deployment):
        try:
            value = self.diagnostics("system/health")
        except (URLError, OSError, ValueError):
            return None
        if value.get("deployment_id") != deployment:
            fail("本机 8765 端口由其他部署使用")
        return value if value.get("api") == value.get("database") == "ready" else None

    def wait(self, status, process, label):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = status()
            if result:
                return result
            if process is not None and process.poll() is not None:
                break
            time.sleep(0.2)
        fail(f"{label}未就绪，请查看对应日志")

    def spawn(self, role, deployment, data, env, owned):
        token = uuid4().hex
        log_path = data / "logs" / f"{role}.log"
        try:
            with log_path.open("ab") as log:
                process = subprocess.Popen(
                    [
                        str(self.bundle / "runtime/python.exe"),
                        "-B",
                        "-X",
                        "utf8",
                        "-m",
                        "upload_platform.portable",
                        "--bundle",
                        str(self.bundle),
                        "--home",
                        str(self.home),
                        "--role",
                        role,
                        "--deployment",
                        deployment,
                        "--token",
                        token,
                    ],
                    cwd=self.bundle,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
        except OSError as exc:
            raise Problem("portable_process", f"无法启动 {role}\n日志位置 {log_path}") from exc
        owned.append((process, token))
        return process

    def start(self, report):
        with self.operation():
            settings = self.settings()
            if settings is None:
                settings = self.configure()
            data = self.data_directory(settings["data"])
            db_file = data / "core/core.sqlite3"
            expected = settings["deployment_id"]
            if expected is not None or db_file.exists():
                deployment = existing_deployment(db_file)
                if expected is not None and expected != deployment:
                    fail(f"原数据库的部署编号不符\n{db_file}")
            else:
                deployment = None
            build_id, engine = self.bundle_engine()
            server = self.record("server", deployment)
            worker = self.record("worker", deployment)
            for record in (server, worker):
                if record and Path(record["bundle"]) != self.bundle:
                    fail("后台正在从另一处解压目录运行，请先退出上传平台")
                if record and self.marker(record["token"]).exists():
                    fail("上传平台正在退出，请稍后再启动")
            if server is None:
                check_port()
            (data / "logs").mkdir(exist_ok=True)
            engine_file = data / "engine.json"
            if server is None and worker is None:
                save_json(
                    engine_file,
                    {
                        "executable": str(engine / ENGINE),
                        "build_id": build_id,
                        "capabilities_file": str(engine / "capabilities.json"),
                        "state_root": str(data / "bilibili-private"),
                        "allow_self_only": True,
                    },
                )
            if deployment is None:
                database = Database(Config(data / "core"))
                try:
                    deployment = database.initialize()
                finally:
                    database.close()
            if expected is None:
                settings["deployment_id"] = deployment
                save_json(self.settings_file, settings)
            env = process_env(self.bundle, data)
            report("正在启动 API", "等待 API 就绪", f"引擎已核对 · {build_id}")
            before = activity_count(db_file)
            owned = []
            try:
                server_process = None
                if server is None:
                    server_process = self.spawn("server", deployment, data, env, owned)

                def server_ready():
                    role = self.record("server", deployment)
                    if not role or (server_process and role["pid"] != server_process.pid):
                        return None
                    return self.health(deployment)

                self.wait(server_ready, server_process, "API 或数据库")
                (platform,) = (
                    entry
                    for entry in self.diagnostics("platforms")["items"]
                    if entry["id"] == "bilibili"
                )
                if (
                    not platform["enabled"]
                    or platform["engine_build"] != build_id
                    or not all(
                        platform["capabilities"].get(key)
                        for key in ("login", "multipart", "self_only", "remote_query")
                    )
                ):
                    fail("API 报告的 B 站引擎构建或能力不符")
                report("API 已就绪", "正在等待 Worker 心跳", f"引擎已就绪 · {build_id}")
                worker_process = None
                if worker is None:
                    worker_process = self.spawn("worker", deployment, data, env, owned)

                def worker_ready():
                    role = self.record("worker", deployment)
                    health = self.health(deployment)
                    return (
                        role
                        and health
                        and any(
                            row["pid"] == role["pid"]
                            and row["id"] == role["worker_id"]
                            and row["active"]
                            for row in health["workers"]
                        )
                    )

                self.wait(worker_ready, worker_process, "Worker 心跳")
            except Exception:
                if owned and activity_count(db_file) == before:
                    for process, token in reversed(owned):
                        if process.poll() is None:
                            self.marker(token).touch()
                            try:
                                process.wait(timeout=8)
                            except subprocess.TimeoutExpired:
                                break  # Leave any draining process and its dependency running.
                raise
            return build_id, platform["available_accounts"], data / "logs"

    def stop(self, report):
        with self.operation():
            settings = self.settings()
            if settings is None:
                return
            deployment = settings["deployment_id"]
            for role in reversed(ROLES):
                record = self.record(role, deployment)
                if record:
                    report(role)
                    self.marker(record["token"]).touch()
                    while live_process(record):
                        time.sleep(0.25)


def check_port():
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", 8765))
    except OSError as exc:
        raise Problem("portable_port", "本机 8765 端口已被占用，请检查正在运行的程序") from exc


def is_deployment_id(value):
    try:
        identifier = UUID(value)
        return identifier.version == 4 and str(identifier) == value
    except (ValueError, TypeError, AttributeError):
        return False


def existing_deployment(file):
    try:
        if not file.is_file() or not file.stat().st_size:
            fail(f"原数据库丢失或为空\n{file}")
        with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)) as conn:
            row = conn.execute(
                "SELECT version, deployment_id FROM schema_version WHERE id='core'"
            ).fetchone()
            if row is None or row[0] != SCHEMA_VERSION or not is_deployment_id(row[1]):
                raise ValueError("Unsupported database")
            return row[1]
    except (sqlite3.DatabaseError, ValueError) as exc:
        raise Problem("portable_database", f"原数据库格式不受支持，请保留原库\n{file}") from exc


def activity_count(file):
    with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)) as conn:
        return tuple(
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "attempts", "account_maintenance", "remote_queries")
        )


def process_env(bundle, data):
    return {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith(("UPLOAD_PLATFORM_", "BUE_", "PYTHON"))
        },
        "UPLOAD_PLATFORM_API_URL": ADDRESS,
        "UPLOAD_PLATFORM_DATA_DIR": str(data / "core"),
        "UPLOAD_PLATFORM_DASHBOARD_DIR": str(bundle / "dashboard"),
        "UPLOAD_PLATFORM_BILIBILI_CONFIG": str(data / "engine.json"),
    }


def serve(portable, role, deployment, token):
    import logging
    import win32api

    from upload_platform.execution.runtime import Worker

    config = Config.from_env()
    if existing_deployment(config.database) != deployment:
        fail(f"原数据库的部署编号不符\n{config.database}")
    flag = Event()
    with exclusive(portable.home / f"{role}.lock"):
        record = {
            "role": role,
            "pid": os.getpid(),
            "bundle": str(portable.bundle),
            "creation_time": process_creation(win32api.GetCurrentProcess()),
            "deployment_id": deployment,
            "token": token,
        }
        database = Database(config)
        try:
            if role == "worker":
                logging.basicConfig(
                    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
                )
                worker = Worker(database, stop=flag)
                record["worker_id"] = worker.store.worker_id
            else:
                worker = None
            save_json(portable.record_path(role), record)

            def stop_requested():
                marker = portable.marker(token)
                while not flag.wait(0.25):
                    if marker.exists():
                        flag.set()

            Thread(target=stop_requested, daemon=True).start()
            if not portable.marker(token).exists():
                if worker is not None:
                    worker.run()
                else:
                    serve_api(config, database, flag)
        finally:
            flag.set()
            database.close()
            portable.marker(token).unlink(missing_ok=True)


def serve_api(config, database, stop):
    import uvicorn
    from starlette.responses import JSONResponse

    from upload_platform.api.app import create_app

    app = create_app(config, database=database)
    guard = Lock()
    writes = 0
    closing = False

    async def managed_app(scope, receive, send):
        nonlocal writes
        if scope["type"] != "http" or scope["method"] not in {"POST", "PATCH", "DELETE", "PUT"}:
            await app(scope, receive, send)
            return
        path = scope["path"]
        finishes_maintenance = (
            path.startswith("/api/v1/account-maintenance/")
            and path.rsplit("/", 1)[-1] in {"process", "complete", "identified"}
        ) or (path.startswith("/api/v1/account-login-sessions/") and path.endswith("/cancel"))
        with guard:
            reject = closing or (stop.is_set() and not finishes_maintenance)
            if not reject:
                writes += 1
        if reject:
            response = JSONResponse({"code": "portable_stopping", "message": "平台正在退出"}, 503)
            await response(scope, receive, send)
            return
        try:
            await app(scope, receive, send)
        finally:
            with guard:
                writes -= 1

    server = uvicorn.Server(
        uvicorn.Config(
            managed_app,
            host="127.0.0.1",
            port=8765,
            proxy_headers=False,
            access_log=False,
            lifespan="on",
        )
    )

    def watch():
        nonlocal closing
        stop.wait()
        sessions = app.state.login_sessions
        while True:
            with guard:
                with sessions.lock:
                    login_running = bool(sessions.threads)
                with database.read() as conn:
                    maintaining = (
                        conn.exec_driver_sql(
                            "SELECT 1 FROM account_maintenance WHERE "
                            "status IN ('STARTING','RUNNING','VERIFYING') "
                            "AND json_extract(execution,'$.exit_confirmed') IS NOT 1 LIMIT 1"
                        ).first()
                        is not None
                    )
                if not writes and not login_running and not maintaining:
                    closing = True
                    server.should_exit = True
                    return
            time.sleep(0.25)

    Thread(target=watch, daemon=True).start()
    server.run()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect-engine", type=Path)
    parser.add_argument("--build-id")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--role", choices=ROLES)
    parser.add_argument("--deployment")
    parser.add_argument("--token")
    args = parser.parse_args()
    try:
        if args.inspect_engine is not None and args.build_id:
            print(json.dumps(engine_capabilities(args.inspect_engine.resolve(), args.build_id)))
        elif (
            args.bundle
            and args.home
            and args.role
            and is_deployment_id(args.deployment)
            and (args.token and re.fullmatch(r"[0-9a-f]{32}", args.token))
        ):
            serve(Portable(args.bundle, args.home), args.role, args.deployment, args.token)
        else:
            parser.error("supply the engine or managed role arguments")
    except Problem as exc:
        print(exc.message, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
