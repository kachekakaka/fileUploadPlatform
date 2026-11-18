from pathlib import Path

from upload_platform.domain.core import Problem


def validate_item(item, deployment, *, b18=None):
    if deployment is None:
        raise Problem("engine_not_configured", "尚未配置 Bilibili 引擎", 409)
    capabilities = deployment.capabilities()
    metadata = item["metadata"]
    options = item["target"]["options"]
    if b18 is None and (
        not capabilities.get("upload_ready")
        or options["publish_mode"] not in capabilities.get("verified_publish_modes", [])
    ):
        raise Problem("UNSUPPORTED_OPTION", "该构建尚未声明经验证的发布方式", 409)
    if any(p["subtitles"] for p in item["parts"]):
        raise Problem("UNSUPPORTED_OPTION", "Bilibili 首版不支持目标引用字幕")
    limits = capabilities.get("metadata_limits", {})
    for field, values in (
        ("title", [metadata["title"]]),
        ("part_title", [p["title"] for p in item["parts"]]),
        ("description", [metadata["description"]]),
        ("tag", metadata["tags"]),
    ):
        limit = limits.get(field)
        if type(limit) is not int or limit < 1 or any(len(value) > limit for value in values):
            raise Problem(
                "UNSUPPORTED_OPTION", "投稿字段超出选定引擎的能力", details={"field": field}
            )
    if (
        not metadata["tags"]
        or any(not tag.strip() or "," in tag for tag in metadata["tags"])
        or len(metadata["tags"]) > limits.get("tag_count", 0)
        or len(item["parts"]) > limits.get("part_count", 0)
    ):
        raise Problem("UNSUPPORTED_OPTION", "标签或分 P 数量不符合引擎能力")
    item["target"]["engine_build_id"] = deployment.build_id


def posting_fields(item, paths):
    metadata = item["metadata"]
    return {
        "title": metadata["title"],
        "description": metadata["description"],
        "tags": metadata["tags"],
        "cover": paths[metadata["cover"]] if metadata["cover"] else None,
        "parts": [
            {"path": paths[part["video_id"]], "title": part["title"]} for part in item["parts"]
        ],
        "options": item["target"]["options"],
    }


def upload_request(task, attempt, account, config):
    from upload_platform.adapters.bilibili.b18 import B18Allowlist, denied
    from upload_platform.adapters.bilibili.protocol import identity

    deployment = config.bilibili
    snapshot = task["snapshot"]
    if snapshot["target"]["engine_build_id"] != deployment.build_id:
        raise Problem("engine_incompatible", "排队快照与当前引擎构建不兼容", 409)
    permit = B18Allowlist.load(config)
    if permit:
        permit.check_task(task, executing=True)
    elif "b18_run_id" in snapshot:
        raise denied("当前 Worker 未配置本轮 B18 清单")
    validate_item(snapshot, deployment, b18=permit)
    if snapshot["target"]["expected_mid"] != account["remote_user_id"]:
        raise Problem("account_identity_changed", "任务冻结的 MID 与账号不符", 409)
    paths = {item["asset_id"]: item["relative_path"] for item in snapshot["assets"]}
    return {
        "type": "upload",
        "identity": identity(attempt),
        "engine_build_id": attempt["execution"]["engine_build_id"],
        "execution_directory": str(Path(attempt["execution"]["result_path"]).parent),
        "state_root": str(deployment.state_root),
        "credential_reference": account["credential_reference"],
        "directory": snapshot["directory"],
        **posting_fields(snapshot, paths),
    }
