from pathlib import PurePosixPath

from pydantic import ValidationError

from upload_platform.application.contracts import Manifest
from upload_platform.domain.core import Problem
from upload_platform.infrastructure.files import asset_info, root_path


def validate_manifest(directory, value, *, fixed_root=False):
    try:
        model = value if isinstance(value, Manifest) else Manifest.model_validate(value)
    except ValidationError as exc:
        raise Problem(
            "invalid_manifest",
            "描述文件不符合契约",
            details={"errors": exc.errors(include_input=False, include_context=False)},
        ) from exc
    root = str(root_path(directory))
    if fixed_root and root != directory:
        raise Problem("invalid_path", "已校验的素材根目录位置已改变")
    manifest = model.normalized()
    assets = {}
    for role, group in manifest["assets"].items():
        for item in group:
            assets[item["id"]] = {
                **item,
                "role": role,
                **asset_info(root, item["path"], fixed_root=fixed_root),
            }
    expanded = []
    fields = ("title", "description", "tags", "cover")
    for unit in manifest["upload_units"]:
        sets = [[v] for v in unit["videos"]] if unit["mode"] == "independent" else [unit["videos"]]
        for video_ids in sets:
            metadata = {"title": None, "description": "", "tags": [], "cover": None}
            sources = {key: "default" for key in fields}
            layers = [("work", manifest["work"]), ("unit", unit["metadata_overrides"])]
            if unit["mode"] == "independent":
                layers.append(("video", assets[video_ids[0]]["metadata"]))
            for label, layer in layers:
                for key in fields:
                    if key in layer:
                        metadata[key] = layer[key]
                        sources[key] = label
            if not metadata["title"] or not metadata["title"].strip():
                raise Problem(
                    "invalid_metadata", "最终投稿标题必须非空", details={"unit": unit["id"]}
                )
            parts, references = [], []
            for video_id in video_ids:
                video = assets[video_id]
                title = video["metadata"].get(
                    "title", PurePosixPath(video["path"].replace("\\", "/")).stem
                )
                if not title.strip():
                    raise Problem("invalid_metadata", "分 P 标题不能为空")
                parts.append(
                    {
                        "video_id": video_id,
                        "title": title,
                        "title_source": "video" if "title" in video["metadata"] else "filename",
                        "subtitles": [
                            {"id": s, "language": assets[s]["language"]} for s in video["subtitles"]
                        ],
                    }
                )
                references.append({"asset_id": video_id, "role": "video", "video_id": video_id})
                references.extend(
                    {"asset_id": s, "role": "subtitle", "video_id": video_id}
                    for s in video["subtitles"]
                )
            if metadata["cover"]:
                references.append(
                    {"asset_id": metadata["cover"], "role": "cover", "video_id": None}
                )
            for target in unit["targets"]:
                if target["enabled"]:
                    expanded.append(
                        {
                            "unit_id": unit["id"],
                            "mode": unit["mode"],
                            "item_key": "group" if unit["mode"] == "multipart" else video_ids[0],
                            "target": target,
                            "metadata": metadata,
                            "sources": sources,
                            "parts": parts,
                            "assets": references,
                            "scope": "group (video description/tags/cover do not override group)"
                            if unit["mode"] == "multipart"
                            else "video",
                        }
                    )
    return {"directory": root, "manifest": manifest, "assets": assets, "expanded": expanded}
