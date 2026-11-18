from contextlib import asynccontextmanager
import asyncio
import json
from typing import Literal

from fastapi import FastAPI, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse, Response
from fastapi.openapi.utils import get_openapi
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from upload_platform.application.contracts import (
    Registration,
    Submission,
    ResolutionRequest,
    ScheduleRequest,
    SchedulePatch,
    Empty,
)
from upload_platform.application.service import Service
from upload_platform.application.remote_queries import RemoteQueries
from upload_platform.application.login_sessions import (
    LoginSessions,
    LoginRequest,
    LoginView,
    LoginList,
    SystemCapabilities,
    summary,
)
from upload_platform.application.accounts import (
    Accounts,
    MaintenanceRequest,
    ProcessIdentity,
    AccountEnabled,
)
from upload_platform.config import Config
from upload_platform.domain.core import Problem, uid, now, TASK_TRANSITIONS
from upload_platform.infrastructure import database as db
from upload_platform.scheduling.runtime import ScheduleService


def create_app(config=None, database=None, service=None):
    config = config or Config.from_env()
    database = database or db.Database(config)
    service = service or Service(database)
    schedules = ScheduleService(service)
    accounts = Accounts(service)
    login_sessions = LoginSessions(accounts)
    remote_queries = RemoteQueries(service)

    @asynccontextmanager
    async def lifespan(app):
        database.initialize()
        accounts.recover_observers()
        login_sessions.start()
        try:
            yield
        finally:
            await asyncio.to_thread(login_sessions.close)
            database.close()

    app = FastAPI(
        title="Upload Platform Core MVP", version="0.1.0", lifespan=lifespan, redirect_slashes=False
    )
    app.state.database, app.state.service = database, service
    app.state.login_sessions = login_sessions

    def error(request, problem):
        return JSONResponse(
            {
                "code": problem.code,
                "message": problem.message,
                "request_id": getattr(request.state, "request_id", uid()),
                "details": problem.details,
            },
            status_code=problem.status,
            headers={"Cache-Control": "no-store"}
            if request.url.path.startswith("/api/v1/account-login-sessions")
            else None,
        )

    @app.middleware("http")
    async def local_only(request, call_next):
        request.state.request_id = uid()
        headers = request.headers
        gui = headers.get("x-upload-platform-client") == "gui-bridge-v1"
        path = request.url.path
        gui_route = path == "/api/v1/account-login-sessions" or path.startswith(
            "/api/v1/account-login-sessions/"
        )
        gui_read = request.method == "GET" and (
            path in ("/api/v1/system/health", "/api/v1/system/capabilities")
            or (path == "/api/v1/accounts" and request.query_params.get("view") == "summary")
        )
        if gui and not (gui_route or gui_read):
            return error(request, Problem("forbidden_source", "界面接入标识不允许访问此入口", 403))
        forbidden = any(
            name in ("origin", "forwarded", "x-real-ip", "via")
            or name.startswith("x-forwarded-")
            or name.startswith("sec-fetch-")
            for name in headers
        )
        if headers.get("host") != config.host or len(headers.getlist("host")) != 1 or forbidden:
            return error(
                request,
                Problem("forbidden_source", "仅接受指定 loopback 地址上的本机非浏览器请求", 403),
            )
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            if headers.get("x-upload-platform-client") != "cli-v1" and not (gui and gui_route):
                return error(request, Problem("forbidden_source", "修改请求缺少客户端标识", 403))
            if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                return error(
                    request, Problem("invalid_content_type", "修改请求只接受 application/json", 400)
                )
        response = await call_next(request)
        if gui_route:
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return error(request, exc)

    @app.exception_handler(HTTPException)
    async def http_handler(request, exc):
        return error(
            request,
            Problem(
                "not_found" if exc.status_code == 404 else "http_error",
                "接口不存在" if exc.status_code == 404 else "请求方法不支持",
                exc.status_code,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        errors = exc.errors()
        status = 400 if any(e["type"] == "json_invalid" for e in errors) else 422
        details = [{"loc": e["loc"], "type": e["type"], "message": e["msg"]} for e in errors]
        return error(
            request, Problem("invalid_request", "请求不符合契约", status, {"errors": details})
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_handler(request, exc):
        return error(
            request, Problem("service_unavailable", "数据库暂不可用，请使用原请求重试", 503)
        )

    @app.exception_handler(ValidationError)
    async def model_handler(request, exc):
        return error(
            request,
            Problem(
                "invalid_request",
                "配置或描述不符合契约",
                422,
                {"errors": exc.errors(include_input=False, include_context=False)},
            ),
        )

    @app.exception_handler(Exception)
    async def unexpected_handler(request, exc):
        return error(request, Problem("internal_error", "服务处理失败，请保留请求标识后重试", 503))

    def key(request):
        return request.headers.get("idempotency-key")

    def response(result):
        status, data = result
        return JSONResponse(data, status_code=status)

    prefix = "/api/v1"

    @app.get(prefix + "/accounts")
    def account_list(view: Literal["summary"] | None = None):
        with database.read() as conn:
            values = db.many(conn, select(db.accounts))
            for value in values:
                value["blocks"] = db.many(
                    conn,
                    select(db.account_blocks).where(db.account_blocks.c.account_id == value["id"]),
                )
            return {
                "items": [summary(x, x["blocks"]) for x in values] if view == "summary" else values
            }

    def client_maintenance(operation_id):
        if accounts.get(operation_id)["execution"].get("login_session"):
            raise Problem("login_server_owned", "界面扫码由服务托管，请使用会话入口", 409)

    @app.get(prefix + "/system/capabilities", response_model=SystemCapabilities)
    def system_capabilities():
        return login_sessions.capabilities()

    @app.post(prefix + "/account-login-sessions", status_code=202, response_model=LoginView)
    def login_create(body: LoginRequest, request: Request):
        return response(login_sessions.create(key(request), body))

    @app.get(prefix + "/account-login-sessions", response_model=LoginList)
    def login_list(active: bool = True, account_id: str | None = None):
        return login_sessions.listing(active, account_id)

    @app.get(prefix + "/account-login-sessions/{session_id}", response_model=LoginView)
    def login_get(session_id: str):
        return login_sessions.view(session_id)

    @app.get(
        prefix + "/account-login-sessions/{session_id}/qr",
        response_class=Response,
        responses={
            200: {"content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}
        },
    )
    def login_image(session_id: str, challenge_id: str):
        return Response(login_sessions.image(session_id, challenge_id), media_type="image/png")

    @app.post(prefix + "/account-login-sessions/{session_id}/cancel", response_model=LoginView)
    def login_cancel(session_id: str, body: Empty, request: Request):
        return response(login_sessions.cancel(session_id, key(request)))

    @app.post(prefix + "/account-login-sessions/{session_id}/resume", response_model=LoginView)
    def login_resume(session_id: str, body: Empty, request: Request):
        return response(login_sessions.resume(session_id, key(request)))

    @app.patch(prefix + "/accounts/{account_id}")
    def account_enabled(account_id: str, body: AccountEnabled, request: Request):
        return response(accounts.enabled(account_id, key(request), body))

    @app.post(prefix + "/account-maintenance", status_code=201)
    def maintenance_create(body: MaintenanceRequest, request: Request):
        return response(accounts.create(key(request), body))

    @app.get(prefix + "/account-maintenance/{operation_id}")
    def maintenance_get(operation_id: str):
        return accounts.get(operation_id)

    @app.post(prefix + "/account-maintenance/{operation_id}/start")
    def maintenance_start(operation_id: str, body: Empty, request: Request):
        client_maintenance(operation_id)
        return response(accounts.start(operation_id, key(request)))

    @app.post(prefix + "/account-maintenance/{operation_id}/process")
    def maintenance_process(operation_id: str, body: ProcessIdentity, request: Request):
        client_maintenance(operation_id)
        return response(accounts.process(operation_id, key(request), body))

    @app.post(prefix + "/account-maintenance/{operation_id}/complete")
    def maintenance_complete(operation_id: str, body: Empty, request: Request):
        client_maintenance(operation_id)
        return response(accounts.complete(operation_id, key(request)))

    @app.post(prefix + "/account-maintenance/{operation_id}/identified")
    def maintenance_identified(operation_id: str, body: Empty, request: Request):
        client_maintenance(operation_id)
        return response(accounts.identified(operation_id, key(request)))

    @app.post(prefix + "/works", status_code=201)
    def register(body: Registration, request: Request):
        return response(service.registration(key(request), body))

    @app.get(prefix + "/works")
    def works(limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        page = service.listing(db.works, limit, cursor)
        page["items"] = [service.work(item["id"]) for item in page["items"]]
        return page

    @app.get(prefix + "/works/{work_id}")
    def work(work_id: str):
        return service.work(work_id)

    @app.post(prefix + "/works/{work_id}/preview")
    def preview(work_id: str, body: Empty):
        return service.preview(work_id)

    @app.post(prefix + "/works/{work_id}/submit", status_code=201)
    def submit(work_id: str, body: Submission, request: Request):
        return response(service.submit(work_id, key(request), body))

    @app.get(prefix + "/works/{work_id}/tasks")
    def work_tasks(work_id: str, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        service.work(work_id)
        return service.listing(db.tasks, limit, cursor, work_id=work_id)

    @app.get(prefix + "/tasks")
    def tasks(
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = None,
        work_id: str | None = None,
        status: str | None = None,
    ):
        if status and status not in TASK_TRANSITIONS:
            raise Problem("invalid_status", "未知任务状态")
        return service.listing(db.tasks, limit, cursor, work_id=work_id, status=status)

    @app.get(prefix + "/tasks/{task_id}")
    def task(task_id: str):
        return service.task(task_id)

    @app.post(prefix + "/tasks/{task_id}/query")
    def task_query(task_id: str, body: Empty, request: Request):
        return response(remote_queries.query(task_id, key(request)))

    @app.get(prefix + "/tasks/{task_id}/queries")
    def task_queries(task_id: str):
        service.task(task_id)
        with database.read() as conn:
            return {
                "items": db.many(
                    conn,
                    select(db.remote_queries)
                    .where(db.remote_queries.c.task_id == task_id)
                    .order_by(db.remote_queries.c.started_at.desc()),
                )
            }

    @app.post(prefix + "/tasks/{task_id}/cancel")
    def cancel(task_id: str, body: Empty, request: Request):
        return response(service.cancel(task_id, key(request), body))

    @app.post(prefix + "/tasks/{task_id}/resubmit", status_code=201)
    def resubmit(task_id: str, body: Submission, request: Request):
        # Reading the task is safe on replay; never access its files before key lookup.
        old = service.task(task_id)
        return response(service.submit(old["work_id"], key(request), body, previous=task_id))

    @app.post(prefix + "/tasks/{task_id}/resolve")
    def resolve(task_id: str, body: ResolutionRequest, request: Request):
        return response(service.resolve(task_id, key(request), body))

    @app.get(prefix + "/tasks/{task_id}/attempts")
    def attempts(task_id: str, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        service.task(task_id)
        return service.listing(db.attempts, limit, cursor, task_id=task_id)

    @app.get(prefix + "/tasks/{task_id}/resolutions")
    def resolutions(task_id: str, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        service.task(task_id)
        return service.listing(db.resolutions, limit, cursor, task_id=task_id)

    @app.get(prefix + "/tasks/{task_id}/events")
    def events(
        task_id: str, after_sequence: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)
    ):
        return service.event_page(task_id, after_sequence, limit)

    @app.get(prefix + "/tasks/{task_id}/events/stream")
    async def stream(task_id: str, request: Request, after_sequence: int = Query(0, ge=0)):
        service.task(task_id)
        try:
            after = int(request.headers.get("last-event-id", after_sequence))
            if after < 0:
                raise ValueError()
        except ValueError as exc:
            raise Problem("invalid_cursor", "Last-Event-ID 必须为非负整数") from exc

        async def generate():
            nonlocal after
            while not await request.is_disconnected():
                page = await asyncio.to_thread(service.event_page, task_id, after, 200)
                for item in page["items"]:
                    yield f"id: {item['sequence']}\nevent: task\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                    after = item["sequence"]
                    # Replay always reads durable events; this marker is advisory delivery progress.
                    with database.transaction() as conn:
                        conn.execute(
                            update(db.outbox)
                            .where(db.outbox.c.event_id == item["id"])
                            .values(published_at=now())
                        )
                if not page["items"]:
                    yield ": keepalive\n\n"
                    await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.post(prefix + "/schedules", status_code=201)
    def create_schedule(body: ScheduleRequest, request: Request):
        return response(schedules.create(key(request), body))

    @app.get(prefix + "/schedules")
    def list_schedules(limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        return service.listing(db.schedules, limit, cursor)

    @app.get(prefix + "/schedules/{schedule_id}")
    def get_schedule(schedule_id: str):
        return schedules.detail(schedule_id)

    @app.patch(prefix + "/schedules/{schedule_id}")
    def patch_schedule(schedule_id: str, body: SchedulePatch, request: Request):
        return response(schedules.patch(schedule_id, key(request), body))

    @app.get(prefix + "/schedules/{schedule_id}/runs")
    def schedule_runs(
        schedule_id: str, limit: int = Query(50, ge=1, le=200), cursor: str | None = None
    ):
        schedules.detail(schedule_id)
        return service.listing(db.runs, limit, cursor, schedule_id=schedule_id)

    @app.get(prefix + "/schedule-runs/{run_id}")
    def schedule_run(run_id: str):
        return schedules.run_detail(run_id)

    @app.get(prefix + "/system/health")
    def health():
        with database.read() as conn:
            conn.execute(select(db.schema_version)).all()
            registered = db.many(conn, select(db.workers))
        return {
            "api": "ready",
            "database": "ready",
            "workers": [
                {
                    **worker,
                    "active": worker["status"] == "RUNNING"
                    and worker["heartbeat_at"] > now() - config.lease_seconds,
                }
                for worker in registered
            ],
        }

    def public_openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema["components"]["schemas"]["ErrorResponse"] = {
            "type": "object",
            "required": ["code", "message", "request_id", "details"],
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "request_id": {"type": "string"},
                "details": {"type": "object", "additionalProperties": True},
            },
            "additionalProperties": False,
        }
        for path, methods in schema["paths"].items():
            for method, operation in methods.items():
                if method in ("post", "patch"):
                    headers = [
                        {
                            "name": "X-Upload-Platform-Client",
                            "in": "header",
                            "required": True,
                            "schema": (
                                {"type": "string", "enum": ["cli-v1", "gui-bridge-v1"]}
                                if path.startswith("/api/v1/account-login-sessions")
                                else {"type": "string", "const": "cli-v1"}
                            ),
                        }
                    ]
                    if not path.endswith("/preview"):
                        headers.append(
                            {
                                "name": "Idempotency-Key",
                                "in": "header",
                                "required": True,
                                "schema": {"type": "string", "format": "uuid"},
                            }
                        )
                    operation.setdefault("parameters", []).extend(headers)
                for status in (400, 403, 404, 409, 410, 422, 503):
                    operation.setdefault("responses", {})[str(status)] = {
                        "description": "统一错误响应",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                            }
                        },
                    }
        app.openapi_schema = schema
        return schema

    app.openapi = public_openapi
    return app
