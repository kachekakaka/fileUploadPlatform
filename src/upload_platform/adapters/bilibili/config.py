from dataclasses import dataclass
import json
from pathlib import Path

from upload_platform.domain.core import Problem


@dataclass(frozen=True)
class EngineConfig:
    executable: Path
    build_id: str
    state_root: Path
    capabilities_file: Path
    allow_test_engine: bool = False
    test_arguments: tuple[str, ...] = ()
    b18_allowlist_file: Path | None = None

    def __post_init__(self):
        if not all(
            isinstance(path, Path) and path.is_absolute()
            for path in (self.executable, self.state_root, self.capabilities_file)
        ):
            raise Problem("configuration", "引擎、私有状态根和能力文件必须使用明确绝对路径")
        if (
            not isinstance(self.build_id, str)
            or not self.build_id
            or type(self.allow_test_engine) is not bool
        ):
            raise Problem("configuration", "引擎构建标识或测试配置无效")
        if self.build_id.endswith("-fixture") and not self.allow_test_engine:
            raise Problem("configuration", "测试引擎不能用于普通运行环境")
        if self.test_arguments and (
            not self.allow_test_engine or not self.build_id.endswith("-fixture")
        ):
            raise Problem("configuration", "额外启动参数仅用于独立测试 Fixture")
        if self.b18_allowlist_file is not None:
            from upload_platform.infrastructure.files import lexical_root

            if not isinstance(self.b18_allowlist_file, Path):
                raise Problem("configuration", "B18 清单必须使用本机绝对路径")
            lexical_root(str(self.b18_allowlist_file))
        if not isinstance(self.test_arguments, tuple) or any(
            not isinstance(x, str) or "\x00" in x for x in self.test_arguments
        ):
            raise Problem("configuration", "测试引擎参数数组无效")

    @property
    def command(self):
        return [str(self.executable), *self.test_arguments]

    @classmethod
    def load(cls, path):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
            for key in ("executable", "state_root", "capabilities_file"):
                value[key] = Path(value[key])
            if value.get("b18_allowlist_file") is not None:
                value["b18_allowlist_file"] = Path(value["b18_allowlist_file"])
            if "test_arguments" in value:
                value["test_arguments"] = tuple(value["test_arguments"])
            return cls(**value)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Problem("configuration", "无法读取 Bilibili 部署配置") from exc

    def capabilities(self):
        try:
            value = json.loads(self.capabilities_file.read_text(encoding="utf-8"))
            if (
                type(value["protocol_version"]) is not int
                or value["protocol_version"] != 1
                or value["engine_build_id"] != self.build_id
                or value["submit_api"] != "web"
            ):
                raise ValueError()
            if not self.executable.is_file():
                raise ValueError()
            return value
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise Problem("engine_incompatible", "部署引擎或固定能力清单不兼容", 409) from exc
