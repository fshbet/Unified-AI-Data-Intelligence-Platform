"""Application settings. All values can be overridden via environment / .env."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_prefix="EDI_", extra="ignore")

    app_name: str = "Enterprise Data Intelligence"
    debug: bool = False
    # Metadata catalog. SQLite by default so the app runs with zero infra; Postgres in docker-compose.
    database_url: str = f"sqlite:///{ROOT / 'data' / 'metadata.db'}"
    redis_url: str | None = None
    secret_key: str = "change-me-in-production-please"
    # Fernet key used to encrypt connector credentials + AI keys at rest. Derived from secret_key if unset.
    encryption_key: str | None = None
    jwt_expire_minutes: int = 60 * 12
    data_dir: Path = ROOT / "data"
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # Query safety
    query_timeout_seconds: int = 30
    max_result_rows: int = 5000
    profile_sample_rows: int = 100_000

    # AI
    ai_max_tool_iterations: int = 12
    ai_request_timeout_seconds: int = 180
    default_admin_email: str = "admin@example.com"
    default_admin_password: str = "admin123"

    # Scheduler
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 60

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def duckdb_dir(self) -> Path:
        return self.data_dir / "duckdb"


settings = Settings()
for _d in (settings.data_dir, settings.uploads_dir, settings.duckdb_dir):
    _d.mkdir(parents=True, exist_ok=True)
