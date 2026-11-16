import json
from uuid import UUID

import httpx

from upload_platform.domain.core import Problem, uid
from upload_platform.infrastructure.files import write_json


class Client:
    def __init__(self, config):
        self.config = config
        self.http = httpx.Client(
            base_url=config.api_url.rstrip("/") + "/api/v1",
            follow_redirects=False,
            trust_env=False,
            timeout=15,
            headers={"X-Upload-Platform-Client": "cli-v1"},
        )

    def close(self):
        self.http.close()

    def read(self, route, *, preview=False):
        try:
            response = self.http.post(route, json={}) if preview else self.http.get(route)
        except httpx.HTTPError as exc:
            raise Problem("service_unavailable", "API 不可达，请检查 server 是否运行", 503) from exc
        return self.decode(response)

    @staticmethod
    def decode(response):
        try:
            body = response.json()
        except ValueError as exc:
            raise Problem("invalid_response", "API 未返回合法 JSON", 503) from exc
        if response.status_code >= 300:
            raise Problem(
                body.get("code", "http_error"),
                body.get("message", "请求失败"),
                response.status_code,
                body.get("details"),
            )
        return body

    def mutate(self, method, route, body):
        request_id = uid()
        log = {
            "request_id": request_id,
            "key": uid(),
            "method": method,
            "route": route,
            "body": body,
            "state": "pending",
        }
        path = self.config.request_logs / (request_id + ".json")
        try:
            write_json(path, log)
        except OSError as exc:
            raise Problem("request_log_failed", "请求日志保存失败，未发送请求", 503) from exc
        return self.send(log, path)

    def send(self, log, path):
        try:
            response = self.http.request(
                log["method"],
                log["route"],
                json=log["body"],
                headers={"Idempotency-Key": log["key"]},
            )
            body = self.decode(response)
        except (httpx.HTTPError, Problem) as exc:
            if isinstance(exc, Problem):
                exc.details["request_id"] = log["request_id"]
                raise
            raise Problem(
                "service_unavailable",
                "未确认请求结果；请使用 request resume 重放",
                503,
                {"request_id": log["request_id"]},
            ) from exc
        log.update(state="confirmed", response=body, http_status=response.status_code)
        try:
            write_json(path, log)
        except OSError as exc:
            raise Problem(
                "request_log_failed",
                "服务器已响应，但本地确认保存失败；可用原请求重放",
                503,
                {"request_id": log["request_id"]},
            ) from exc
        return {"request_id": log["request_id"], "operation_result": body}

    def resume(self, request_id):
        try:
            identifier = str(UUID(request_id))
            path = self.config.request_logs / (identifier + ".json")
            log = json.loads(path.read_text(encoding="utf-8"))
            if (
                log["request_id"] != identifier
                or log["method"] not in ("POST", "PATCH")
                or not log["route"].startswith("/")
                or log["route"].startswith("//")
                or ":" in log["route"]
            ):
                raise ValueError()
            UUID(log["key"])
        except (OSError, ValueError, KeyError) as exc:
            raise Problem("invalid_request_log", "请求日志不存在或不合法") from exc
        result = self.send(log, path)
        original = result["operation_result"]
        ids = [item["task_id"] for item in original.get("tasks", [])]
        if "task_id" in original:
            ids.append(original["task_id"])
        result["current_tasks"] = [self.read("/tasks/" + task_id) for task_id in ids]
        return result
