from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit

from upload_platform.domain.core import Problem


@dataclass(frozen=True)
class Config:
    data_dir: Path
    api_url: str = "http://127.0.0.1:8765"
    heartbeat_seconds: float = 10
    lease_seconds: float = 30
    handshake_timeout: float = 10
    cancel_timeout: float = 10
    execution_timeout: float = 300
    poll_seconds: float = 0.25
    busy_timeout_ms: int = 5000
    requests_dir: Path | None = None
    bilibili: object | None = None
    manifest_roots: tuple[Path, ...] = ()
    asset_roots: tuple[Path, ...] = ()
    dashboard_dir: Path | None = None

    def __post_init__(self):
        if not self.data_dir.is_absolute():
            raise Problem("configuration", "运行目录必须是明确的本机绝对路径")
        from upload_platform.infrastructure.files import lexical_root

        lexical_root(str(self.data_dir))
        if self.bilibili is not None:
            from upload_platform.adapters.bilibili.config import EngineConfig

            if not isinstance(self.bilibili, EngineConfig):
                raise Problem("configuration", "Bilibili 部署配置类型无效")
            state = self.bilibili.state_root.resolve()
            data = self.data_dir.resolve()
            if state == data or state.is_relative_to(data) or data.is_relative_to(state):
                raise Problem("configuration", "引擎私有状态目录必须与 Core 数据目录分离")
        if self.requests_dir is not None:
            lexical_root(str(self.requests_dir))
        for directory in (*self.manifest_roots, *self.asset_roots):
            lexical_root(str(directory))
        if self.dashboard_dir is not None:
            lexical_root(str(self.dashboard_dir))
        parsed = urlsplit(self.api_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or not parsed.port
        ):
            raise Problem("configuration", "API 地址必须为 http://127.0.0.1:<端口>")
        if not 0 < self.heartbeat_seconds < self.lease_seconds:
            raise Problem("configuration", "租期必须大于正数心跳间隔")
        if (
            min(
                self.handshake_timeout,
                self.cancel_timeout,
                self.execution_timeout,
                self.poll_seconds,
            )
            <= 0
        ):
            raise Problem("configuration", "等待超时和轮询周期必须大于零")

    @property
    def database(self):
        return self.data_dir / "core.sqlite3"

    @property
    def host(self):
        return urlsplit(self.api_url).netloc

    @property
    def request_logs(self):
        return self.requests_dir or self.data_dir / "requests"

    @classmethod
    def from_env(cls):
        directory = os.environ.get("UPLOAD_PLATFORM_DATA_DIR")
        if not directory:
            raise Problem("configuration", "请设置 UPLOAD_PLATFORM_DATA_DIR 为仓外运行目录")
        values = {
            "data_dir": Path(directory),
            "api_url": os.getenv("UPLOAD_PLATFORM_API_URL", "http://127.0.0.1:8765"),
        }
        for field in (
            "heartbeat_seconds",
            "lease_seconds",
            "handshake_timeout",
            "cancel_timeout",
            "execution_timeout",
            "poll_seconds",
        ):
            value = os.getenv("UPLOAD_PLATFORM_" + field.upper())
            if value is not None:
                values[field] = float(value)
        if os.getenv("UPLOAD_PLATFORM_REQUESTS_DIR"):
            values["requests_dir"] = Path(os.environ["UPLOAD_PLATFORM_REQUESTS_DIR"])
        for field in ("manifest_roots", "asset_roots"):
            paths = os.getenv("UPLOAD_PLATFORM_" + field.upper(), "")
            values[field] = tuple(Path(path) for path in paths.split(os.pathsep) if path)
        if os.getenv("UPLOAD_PLATFORM_DASHBOARD_DIR"):
            values["dashboard_dir"] = Path(os.environ["UPLOAD_PLATFORM_DASHBOARD_DIR"])
        if os.getenv("UPLOAD_PLATFORM_BILIBILI_CONFIG"):
            from upload_platform.adapters.bilibili.config import EngineConfig

            values["bilibili"] = EngineConfig.load(os.environ["UPLOAD_PLATFORM_BILIBILI_CONFIG"])
        return cls(**values)
