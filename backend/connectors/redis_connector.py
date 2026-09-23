"""Redis connector. Redis is not relational: keys are discovered by pattern and normalised into
one DuckDB table per key *prefix* (e.g. `order:*` → table `order`) so hashes/JSON become rows,
plus a generic `redis_keys` table with (key, type, ttl, value) for everything else."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Iterator

import pandas as pd

from backend.connectors.base import ConfigField
from backend.connectors.materialized import MaterializedConnector
from backend.connectors.registry import register


@register
class RedisConnector(MaterializedConnector):
    type_key = "redis"
    display_name = "Redis"
    category = "nosql"
    config_fields = [
        ConfigField("host", "Host", default="localhost"),
        ConfigField("port", "Port", type="integer", default=6379),
        ConfigField("db", "DB index", type="integer", required=False, default=0),
        ConfigField("password", "Password", type="password", required=False),
        ConfigField("pattern", "Key pattern", required=False, default="*"),
        ConfigField("max_keys", "Max keys to scan", type="integer", required=False, default=50000),
    ]

    def _client(self):
        import redis

        c = self.config
        return redis.Redis(host=c.get("host", "localhost"), port=int(c.get("port") or 6379), db=int(c.get("db") or 0), password=c.get("password") or None, decode_responses=True, socket_timeout=10)

    def test_connection(self) -> dict[str, Any]:
        try:
            info = self._client().info("server")
            return {"ok": True, "message": f"Redis {info.get('redis_version')}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    def list_databases(self) -> list[str]:
        return [str(i) for i in range(16)]

    def load_frames(self) -> Iterator[tuple[str, pd.DataFrame]]:
        r = self._client()
        pattern = self.config.get("pattern") or "*"
        max_keys = int(self.config.get("max_keys") or 50000)
        groups: dict[str, list[dict]] = defaultdict(list)
        generic: list[dict] = []
        n = 0
        for key in r.scan_iter(match=pattern, count=1000):
            n += 1
            if n > max_keys:
                break
            t = r.type(key)
            prefix = _prefix(key)
            if t == "hash":
                groups[prefix].append({"_key": key, **r.hgetall(key)})
            elif t == "ReJSON-RL":
                try:
                    doc = r.execute_command("JSON.GET", key)
                    groups[prefix].append({"_key": key, **_flatten(json.loads(doc))})
                except Exception:  # noqa: BLE001
                    generic.append({"key": key, "type": t, "ttl": r.ttl(key), "value": None})
            elif t == "string":
                val = r.get(key)
                parsed = _try_json(val)
                if isinstance(parsed, dict):
                    groups[prefix].append({"_key": key, **_flatten(parsed)})
                else:
                    generic.append({"key": key, "type": t, "ttl": r.ttl(key), "value": val})
            elif t == "list":
                for i, v in enumerate(r.lrange(key, 0, -1)):
                    generic.append({"key": key, "type": t, "ttl": r.ttl(key), "value": v, "index": i})
            elif t == "set":
                for v in r.smembers(key):
                    generic.append({"key": key, "type": t, "ttl": r.ttl(key), "value": v})
            elif t == "zset":
                for v, score in r.zrange(key, 0, -1, withscores=True):
                    generic.append({"key": key, "type": t, "ttl": r.ttl(key), "value": v, "score": score})
            else:
                generic.append({"key": key, "type": t, "ttl": r.ttl(key), "value": None})
        for prefix, rows in groups.items():
            yield prefix, _coerce(pd.DataFrame(rows))
        if generic:
            yield "redis_keys", pd.DataFrame(generic)


def _prefix(key: str) -> str:
    part = re.split(r"[:/.]", key)[0] if re.search(r"[:/.]", key) else "keys"
    part = re.sub(r"[^a-zA-Z0-9_]", "_", part).lower() or "keys"
    return part


def _try_json(val: str) -> Any:
    if val and val[:1] in "{[":
        try:
            return json.loads(val)
        except ValueError:
            return val
    return val


def _flatten(d: dict, parent: str = "", sep: str = "_") -> dict:
    out: dict = {}
    for k, v in d.items():
        nk = f"{parent}{sep}{k}" if parent else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, nk, sep))
        elif isinstance(v, list):
            out[nk] = json.dumps(v)
        else:
            out[nk] = v
    return out


def _coerce(df: pd.DataFrame) -> pd.DataFrame:
    for c in df.columns:
        if df[c].dtype == object or pd.api.types.is_string_dtype(df[c]):
            try:
                df[c] = pd.to_numeric(df[c])
            except (ValueError, TypeError):
                pass
    return df
