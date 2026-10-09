"""Application settings. All values can be overridden via environment / .env."""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# Values that must never survive into a production deployment. `secret_key` signs JWTs AND
# derives the Fernet key that encrypts every stored credential, so shipping the default means
# anyone holding this source can forge an admin token and decrypt the credential vault.
INSECURE_DEFAULTS = {"change-me-in-production-please", "admin123", "changeme", ""}

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_prefix="EDI_", extra="ignore")

    app_name: str = "Enterprise Data Intelligence"
    debug: bool = False
    # Metadata catalog. SQLite by default so the app runs with zero infra; Postgres in docker-compose.
    database_url: str = f"sqlite:///{ROOT / 'data' / 'metadata.db'}"
    redis_url: str | None = None
    # Deliberately empty. In production this must be supplied; elsewhere one is generated and
    # persisted on first run (see _check_secrets).
    secret_key: str = ""
    environment: str = "development"  # development | staging | production
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
    # Empty means "generate a random one and print it once". A fixed default is a published
    # credential for every deployment that forgets to change it.
    default_admin_password: str = ""

    # Python analysis tool. OFF by default.
    #
    # The in-process guard (import allow-list + stripped builtins) is NOT a security boundary:
    # `getattr` and `type` survive it, so ().__class__.__base__.__subclasses__() reaches Popen
    # and gives LLM-authored code host RCE. Verified. Enable this only when the process runs
    # inside a container with no network, a read-only filesystem and dropped capabilities.
    python_analysis_enabled: bool = False
    python_analysis_timeout_seconds: int = 30

    # Data retention (GDPR Art. 5(1)(e) storage limitation). See workers/retention.py.
    query_preview_retention_days: int = 30   # stored result rows; the query text is kept
    anon_vault_retention_hours: int = 24     # de-anonymisation key material
    audit_retention_days: int = 365

    # Scheduler
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 60

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @model_validator(mode="after")
    def _check_secrets(self) -> "Settings":
        if self.is_production:
            # Refuse to start rather than run with a guessable key. A warning would be ignored.
            if self.secret_key in INSECURE_DEFAULTS:
                raise ValueError(
                    "EDI_SECRET_KEY must be set to a unique random value in production. "
                    "Generate one with: python -c \"import secrets;print(secrets.token_urlsafe(48))\"")
            if self.default_admin_password in INSECURE_DEFAULTS:
                raise ValueError("EDI_DEFAULT_ADMIN_PASSWORD must be set in production")
            if any(o in {"*", "http://localhost:3000"} for o in self.cors_origins):
                raise ValueError("EDI_CORS_ORIGINS still contains a development origin")
        if self.secret_key in INSECURE_DEFAULTS:
            self.secret_key = _persist_generated_secret()
        return self

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def duckdb_dir(self) -> Path:
        return self.data_dir / "duckdb"


def _persist_generated_secret() -> str:
    """Generate a secret key once and write it to .env.

    Regenerating per start would silently make every previously stored credential undecryptable,
    so it is written to disk and loaded from there afterwards.
    """
    env = ROOT / ".env"
    generated = secrets.token_urlsafe(48)
    try:
        existing = env.read_text(encoding="utf-8") if env.exists() else ""
        if "EDI_SECRET_KEY=" not in existing:
            with env.open("a", encoding="utf-8") as f:
                f.write(("" if existing.endswith("\n") or not existing else "\n")
                        + f"EDI_SECRET_KEY={generated}\n")
            log.warning("No EDI_SECRET_KEY was set. Generated one and wrote it to %s. "
                        "Keep this file: losing it makes every stored credential unreadable.", env)
    except OSError:
        log.error("No EDI_SECRET_KEY set and .env is not writable. Using an EPHEMERAL key — "
                  "every stored credential will be unreadable after restart.")
    return generated


settings = Settings()
for _d in (settings.data_dir, settings.uploads_dir, settings.duckdb_dir):
    _d.mkdir(parents=True, exist_ok=True)
