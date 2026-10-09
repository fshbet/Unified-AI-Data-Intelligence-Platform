"""Encrypt secrets that were written to the database in plaintext.

Before this, `SECRET_FIELDS` was an exact-match set, so any field not literally named
`password`/`api_key`/`token`/`secret`/`auth_token`/`bearer_token` was stored unencrypted —
including `client_secret`, `pat_secret`, `dsn`, `access_token` and `credentials_json`.

Run once after upgrading:

    python scripts/encrypt_existing_secrets.py --dry-run     # show what would change
    python scripts/encrypt_existing_secrets.py

Idempotent: a value that is already ciphertext is left alone.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, select  # noqa: E402

from backend.core.crypto import encrypt, is_secret_field, looks_encrypted  # noqa: E402
from backend.core.db import SessionLocal, engine  # noqa: E402
from backend.metadata.models import AIProviderConfig, DataSource  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tables = set(inspect(engine).get_table_names())
    changed = 0
    with SessionLocal() as db:
        for src in db.scalars(select(DataSource)).all():
            for attr in ("config", "auth_config"):
                cfg = dict(getattr(src, attr, None) or {})
                if not cfg:
                    continue
                dirty = False
                for k, v in cfg.items():
                    if is_secret_field(k) and isinstance(v, str) and v and not looks_encrypted(v):
                        print(f"  source {src.name!r}.{attr}.{k}: plaintext -> encrypted")
                        if not args.dry_run:
                            cfg[k] = encrypt(v)
                        dirty = changed = True
                if dirty and not args.dry_run:
                    setattr(src, attr, cfg)

        if "ai_provider_configs" in tables or AIProviderConfig.__tablename__ in tables:
            for prov in db.scalars(select(AIProviderConfig)).all():
                if prov.api_key and not looks_encrypted(prov.api_key):
                    print(f"  ai provider {prov.name!r}.api_key: plaintext -> encrypted")
                    if not args.dry_run:
                        prov.api_key = encrypt(prov.api_key)
                    changed = True

        if args.dry_run:
            db.rollback()
            print(f"\n[dry run] {'nothing to do' if not changed else 'changes listed above'}")
        else:
            db.commit()
            print(f"\nDone. {'No plaintext secrets found.' if not changed else 'Secrets encrypted.'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
