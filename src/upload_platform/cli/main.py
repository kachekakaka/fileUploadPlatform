import argparse
import json
import logging
from pathlib import Path
import sys
import time

import httpx

from upload_platform.cli.client import Client
from upload_platform.config import Config
from upload_platform.domain.core import Problem
from upload_platform.infrastructure.files import prepare


def execution_args(parser):
    parser.add_argument("--at")
    parser.add_argument("--max-lateness-seconds", type=int)
    parser.add_argument("--max-attempts", type=int)
    parser.add_argument("--retry-delays-seconds", help="逗号分隔的秒数；空字符串表示不重试")


def schedule_args(parser, *, required=False):
    parser.add_argument("--dir", required=required)
    parser.add_argument("--kind", choices=["daily", "weekly"], required=required)
    parser.add_argument("--time", required=required)
    parser.add_argument("--weekday", type=int)
    parser.add_argument("--timezone")
    parser.add_argument("--max-lateness-seconds", type=int)
    parser.add_argument("--max-attempts", type=int)
    parser.add_argument("--retry-delays-seconds")


def page_args(parser):
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--cursor")


def parser():
    root = argparse.ArgumentParser(prog="upload-platform", description="本机作品上传编排")
    root.add_argument("--json", action="store_true")
    groups = root.add_subparsers(dest="group", required=True)
    for name in ("server", "worker", "doctor"):
        groups.add_parser(name)
    work = groups.add_parser("work").add_subparsers(dest="action", required=True)
    for name in ("prepare", "ready"):
        work.add_parser(name).add_argument("--dir", required=True)
    register = work.add_parser("register")
    location = register.add_mutually_exclusive_group(required=True)
    location.add_argument("--dir")
    location.add_argument("--manifest")
    for name in ("preview", "show", "upload", "status"):
        command = work.add_parser(name)
        command.add_argument("id")
        if name == "upload":
            execution_args(command)
        if name == "status":
            page_args(command)
    listing = work.add_parser("list")
    page_args(listing)
    task = groups.add_parser("task").add_subparsers(dest="action", required=True)
    listing = task.add_parser("list")
    page_args(listing)
    listing.add_argument("--work-id")
    listing.add_argument("--status")
    for name in (
        "status",
        "cancel",
        "reupload",
        "reschedule",
        "resolve",
        "events",
        "attempts",
        "resolutions",
        "query",
        "queries",
    ):
        command = task.add_parser(name)
        command.add_argument("id")
        if name in ("reupload", "reschedule"):
            execution_args(command)
        if name == "resolve":
            command.add_argument(
                "--conclusion", choices=["succeeded", "failed", "uncertain"], required=True
            )
            command.add_argument("--reason", required=True)
            command.add_argument("--evidence", action="append", default=[])
        if name == "events":
            command.add_argument("--follow", action="store_true")
            command.add_argument("--after-sequence", type=int, default=0)
            command.add_argument("--limit", type=int, default=50)
        if name in ("attempts", "resolutions"):
            page_args(command)
    schedule = groups.add_parser("schedule").add_subparsers(dest="action", required=True)
    schedule_args(schedule.add_parser("create"), required=True)
    listing = schedule.add_parser("list")
    page_args(listing)
    for name in ("show", "update", "enable", "disable", "runs", "run"):
        command = schedule.add_parser(name)
        command.add_argument("id")
        if name == "update":
            schedule_args(command)
        if name == "runs":
            page_args(command)
    requests = groups.add_parser("request").add_subparsers(dest="action", required=True)
    requests.add_parser("resume").add_argument("id")
    accounts = groups.add_parser("account").add_subparsers(dest="action", required=True)
    accounts.add_parser("list")
    login = accounts.add_parser("login")
    login.add_argument("--account-id")
    login.add_argument("--import-file")
    for name in ("validate", "renew", "enable", "disable"):
        accounts.add_parser(name).add_argument("id")
    accounts.add_parser("resume").add_argument("id")
    return root


def retry_values(args):
    budget, delays = (
        getattr(args, "max_attempts", None),
        getattr(args, "retry_delays_seconds", None),
    )
    if budget is None and delays is None:
        return None
    if delays is None:
        delays = [] if budget == 1 else [30, 120]
    else:
        try:
            delays = [int(x) for x in delays.split(",") if x.strip()]
        except ValueError as exc:
            raise Problem("invalid_retry", "重试间隔应为逗号分隔的整数") from exc
    return {
        "max_attempts": budget if budget is not None else len(delays) + 1,
        "retry_delays_seconds": delays,
    }


def execution(args):
    body = {"execution_type": "scheduled" if args.at else "immediate"}
    if args.at:
        body["scheduled_at"] = args.at
    if args.max_lateness_seconds is not None:
        body["max_lateness_seconds"] = args.max_lateness_seconds
    policy = retry_values(args)
    if policy is not None:
        body["retry_policy"] = policy
    return body


def schedule_values(args):
    result = {}
    for cli, api in [
        ("dir", "parent_directory"),
        ("kind", "kind"),
        ("time", "local_time"),
        ("weekday", "weekday"),
        ("timezone", "timezone"),
        ("max_lateness_seconds", "max_lateness_seconds"),
    ]:
        value = getattr(args, cli, None)
        if value is not None:
            result[api] = str(Path(value).absolute()) if cli == "dir" else value
    if result.get("kind") == "daily":
        result["weekday"] = None
    retry = retry_values(args)
    if retry is not None:
        result["retry_policy"] = retry
    return result


def output(value, *, stream=None):
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream or sys.stdout, flush=True)


def page_route(route, args, extra=None):
    params = {"limit": args.limit}
    if getattr(args, "cursor", None):
        params["cursor"] = args.cursor
    params.update({k: v for k, v in (extra or {}).items() if v is not None})
    return route + "?" + str(httpx.QueryParams(params))


def show_preview(client, work_id, args):
    preview = client.read(f"/works/{work_id}/preview", preview=True)
    # Keep stdout a single result object in --json mode; preview is still displayed.
    output(
        {
            "preview": preview["expanded"],
            "previously_submitted_not_in_preview": preview["previously_submitted_not_in_preview"],
            "note": "create_new_submission 将创建新投稿；reuse_current 返回已有任务原快照。改变单元/模式/视频标识或目标会改变投稿身份。",
        },
        stream=sys.stderr if args.json else sys.stdout,
    )
    return {"directory": preview["directory"], "manifest": preview["manifest"]}


def follow_events(client, args):
    after = args.after_sequence
    while True:
        try:
            with client.http.stream(
                "GET",
                f"/tasks/{args.id}/events/stream",
                headers={"Last-Event-ID": str(after)},
                timeout=None,
            ) as response:
                if response.status_code != 200:
                    response.read()
                    client.decode(response)
                pending_id = None
                for line in response.iter_lines():
                    if line.startswith("id: "):
                        pending_id = int(line[4:])
                    elif line.startswith("data: "):
                        event = json.loads(line[6:])
                        if pending_id is not None and pending_id > after:
                            output(event)
                            after = pending_id
        except httpx.HTTPError:
            print("事件连接中断，使用最后序号重连", file=sys.stderr)
            time.sleep(1)


def dispatch(args, client):
    group, action = args.group, getattr(args, "action", None)
    if group == "task" and action in ("query", "queries"):
        if action == "queries":
            return client.read(f"/tasks/{args.id}/queries")
        client.http.timeout = httpx.Timeout(120)
        return client.mutate("POST", f"/tasks/{args.id}/query", {})
    if group == "account":
        if action == "list":
            return client.read("/accounts")
        if action in ("enable", "disable"):
            return client.mutate("PATCH", "/accounts/" + args.id, {"enabled": action == "enable"})
        from upload_platform.cli.accounts import maintain

        if action == "resume":
            return maintain(client, "login", operation_id=args.id)
        return maintain(
            client,
            action,
            getattr(args, "account_id", None) if action == "login" else args.id,
            import_file=str(Path(args.import_file).absolute())
            if getattr(args, "import_file", None)
            else None,
        )
    if group == "doctor":
        return client.read("/system/health")
    if group == "request":
        return client.resume(args.id)
    if group == "work":
        if action == "register":
            path = (
                Path(args.manifest).absolute()
                if args.manifest
                else Path(args.dir).absolute() / "upload.yaml"
            )
            if path.name != "upload.yaml":
                raise Problem("invalid_manifest_path", "描述文件名称必须为 upload.yaml")
            manifest = prepare(path.parent)
            return client.mutate(
                "POST", "/works", {"directory": str(path.parent), "manifest": manifest}
            )
        if action == "list":
            return client.read(page_route("/works", args))
        if action == "show":
            return client.read("/works/" + args.id)
        if action == "status":
            return {
                "work": client.read("/works/" + args.id),
                "tasks": client.read(page_route(f"/works/{args.id}/tasks", args)),
            }
        if action == "preview":
            return client.read(f"/works/{args.id}/preview", preview=True)
        if action == "upload":
            description = show_preview(client, args.id, args)
            return client.mutate(
                "POST", f"/works/{args.id}/submit", {**execution(args), **description}
            )
    if group == "task":
        if action == "list":
            return client.read(
                page_route("/tasks", args, {"work_id": args.work_id, "status": args.status})
            )
        if action == "status":
            return client.read("/tasks/" + args.id)
        if action in ("attempts", "resolutions"):
            return client.read(page_route(f"/tasks/{args.id}/{action}", args))
        if action == "cancel":
            return client.mutate("POST", f"/tasks/{args.id}/cancel", {})
        if action in ("reupload", "reschedule"):
            if (action == "reschedule") != bool(args.at):
                raise Problem("invalid_time", "reschedule 必须提供 --at；立即重传使用 reupload")
            task = client.read("/tasks/" + args.id)
            description = show_preview(client, task["work_id"], args)
            return client.mutate(
                "POST", f"/tasks/{args.id}/resubmit", {**execution(args), **description}
            )
        if action == "resolve":
            task = client.read("/tasks/" + args.id)
            return client.mutate(
                "POST",
                f"/tasks/{args.id}/resolve",
                {
                    "conclusion": args.conclusion,
                    "reason": args.reason,
                    "evidence_refs": args.evidence,
                    "expected_task_version": task["version"],
                },
            )
        if action == "events":
            if args.follow:
                follow_events(client, args)
                return None
            return client.read(
                f"/tasks/{args.id}/events?after_sequence={args.after_sequence}&limit={args.limit}"
            )
    if group == "schedule":
        if action == "create":
            return client.mutate("POST", "/schedules", schedule_values(args))
        if action == "list":
            return client.read(page_route("/schedules", args))
        if action == "run":
            return client.read("/schedule-runs/" + args.id)
        if action == "show":
            return client.read("/schedules/" + args.id)
        if action == "runs":
            return client.read(page_route(f"/schedules/{args.id}/runs", args))
        old = client.read("/schedules/" + args.id)
        fields = schedule_values(args) if action == "update" else {"enabled": action == "enable"}
        return client.mutate(
            "PATCH", "/schedules/" + args.id, {**fields, "expected_version": old["version"]}
        )
    raise Problem("invalid_command", "未知命令")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    args = parser().parse_args([a for a in argv if a != "--json"])
    args.json = json_mode
    client = None
    try:
        if args.group == "work" and args.action in ("prepare", "ready"):
            output(prepare(Path(args.dir).absolute(), ready=args.action == "ready"))
            return
        config = Config.from_env()
        if args.group == "server":
            import uvicorn
            from upload_platform.api.app import create_app

            uvicorn.run(
                create_app(config),
                host="127.0.0.1",
                port=int(config.host.rsplit(":", 1)[1]),
                proxy_headers=False,
                access_log=False,
            )
            return
        if args.group == "worker":
            from upload_platform.execution.runtime import Worker
            from upload_platform.infrastructure.database import Database

            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
            Worker(Database(config)).run()
            return
        client = Client(config)
        result = dispatch(args, client)
        if result is not None:
            output(result)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Problem as exc:
        output(
            {"code": exc.code, "message": exc.message, "details": exc.details}, stream=sys.stderr
        )
        raise SystemExit(3 if exc.status >= 500 else 2)
    except (ValueError, OSError) as exc:
        output(
            {
                "code": "local_error",
                "message": "本地配置或文件操作失败",
                "type": type(exc).__name__,
            },
            stream=sys.stderr,
        )
        raise SystemExit(2)
    finally:
        if client:
            client.close()
