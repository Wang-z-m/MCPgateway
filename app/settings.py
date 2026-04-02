from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    api_key: str = "dev-api-key"
    admin_api_key: str = "dev-admin-key"
    config_dir: Path = Path("configs/tools")
    openapi_dir: Path = Path("configs/openapi")
    sqlite_path: Path = Path("data/gateway.db")
    log_level: str = "INFO"
    rate_limit_window_seconds: int = 60
    rate_limit_max_requests: int = 60
    session_ttl_seconds: int = 1800
    project_name: str = "mcp-smart-api-gateway"
    project_version: str = "0.1.0"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(Path(".env"))
        return cls(
            host=os.getenv("MCP_GATEWAY_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_GATEWAY_PORT", "8000")),
            api_key=os.getenv("MCP_GATEWAY_API_KEY", "dev-api-key"),
            admin_api_key=os.getenv("MCP_GATEWAY_ADMIN_API_KEY", "dev-admin-key"),
            config_dir=Path(os.getenv("MCP_GATEWAY_CONFIG_DIR", "configs/tools")),
            openapi_dir=Path(os.getenv("MCP_GATEWAY_OPENAPI_DIR", "configs/openapi")),
            sqlite_path=Path(os.getenv("MCP_GATEWAY_SQLITE_PATH", "data/gateway.db")),
            log_level=os.getenv("MCP_GATEWAY_LOG_LEVEL", "INFO"),
            rate_limit_window_seconds=int(
                os.getenv("MCP_GATEWAY_RATE_LIMIT_WINDOW_SECONDS", "60")
            ),
            rate_limit_max_requests=int(
                os.getenv("MCP_GATEWAY_RATE_LIMIT_MAX_REQUESTS", "60")
            ),
            session_ttl_seconds=int(
                os.getenv("MCP_GATEWAY_SESSION_TTL_SECONDS", "1800")
            ),
            project_name=os.getenv("MCP_GATEWAY_NAME", "mcp-smart-api-gateway"),
            project_version=os.getenv("MCP_GATEWAY_VERSION", "0.1.0"),
        )

    def with_base_dir(self, base_dir: Path) -> "Settings":
        return Settings(
            host=self.host,
            port=self.port,
            api_key=self.api_key,
            admin_api_key=self.admin_api_key,
            config_dir=(base_dir / self.config_dir).resolve(),
            openapi_dir=(base_dir / self.openapi_dir).resolve(),
            sqlite_path=(base_dir / self.sqlite_path).resolve(),
            log_level=self.log_level,
            rate_limit_window_seconds=self.rate_limit_window_seconds,
            rate_limit_max_requests=self.rate_limit_max_requests,
            session_ttl_seconds=self.session_ttl_seconds,
            project_name=self.project_name,
            project_version=self.project_version,
        )
