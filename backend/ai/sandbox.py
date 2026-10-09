"""Controlled Python analysis environment. Runs in a subprocess with a restricted import whitelist,
no network, a wall-clock timeout and result size cap. Input data is passed as JSON and exposed as
pandas DataFrames; the script must set `result` (dict/list/scalar) which is returned.

SECURITY: this is a speed bump, not a sandbox, and it is DISABLED BY DEFAULT.
The builtins filter removes open/exec/eval/__import__ but leaves attribute traversal, so
`().__class__.__base__.__subclasses__()` reaches subprocess.Popen and executes arbitrary
commands as this process. That has been verified. Treat `run_analysis` as "run untrusted code
on this host" and enable it only inside a container with network_mode: none, a read-only
filesystem, dropped capabilities and a memory/pid limit."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

RUNNER = r'''
import builtins, json, sys, math, statistics, re, datetime, itertools, collections, functools
import pandas as pd, numpy as np
payload = json.loads(sys.stdin.read())
data = {k: pd.DataFrame(v["rows"], columns=v["columns"]) for k, v in payload["data"].items()}
ALLOWED = {"pandas","numpy","scipy","sklearn","math","statistics","json","re","datetime","itertools","collections","functools","decimal","fractions","random","string","operator"}
_real_import = builtins.__import__
def _guard(name, globals=None, locals=None, fromlist=(), level=0):
    # only imports issued *from the analysis code* are restricted; library-internal imports pass through
    if globals is not None and globals.get("__name__") == "__analysis__" and name.split(".")[0] not in ALLOWED:
        raise ImportError(f"import of '{name}' is not allowed in the analysis sandbox")
    return _real_import(name, globals, locals, fromlist, level)
_compile, _exec = compile, exec
env = {"__name__": "__analysis__", "__builtins__": {**{k: getattr(builtins, k) for k in dir(builtins) if k not in {"open", "exec", "eval", "compile", "input", "breakpoint", "exit", "quit", "__import__"}}, "__import__": _guard},
       "pd": pd, "np": np, "data": data, "result": None, "math": math, "statistics": statistics, "json": json, "re": re, "datetime": datetime, "itertools": itertools, "collections": collections, "functools": functools}
try:
    code = _compile(payload["code"], "<analysis>", "exec")
    _exec(code, env)
    out = env.get("result")
    def _default(o):
        if hasattr(o, "to_dict"):
            return o.to_dict(orient="records") if hasattr(o, "columns") else o.to_dict()
        if hasattr(o, "tolist"):
            return o.tolist()
        if hasattr(o, "isoformat"):
            return o.isoformat()
        if isinstance(o, float) and (o != o):
            return None
        return str(o)
    s = json.dumps({"ok": True, "result": out}, default=_default)
    if len(s) > 200000:
        s = json.dumps({"ok": False, "error": "result too large (>200KB); aggregate further"})
    sys.stdout.write(s)
except Exception as e:
    sys.stdout.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
'''


class SandboxDisabled(RuntimeError):
    pass


def run_analysis(code: str, data: dict[str, dict[str, Any]], timeout: int | None = None) -> dict[str, Any]:
    """data: {"name": {"columns": [...], "rows": [[...]]}}

    Refuses unless explicitly enabled. The subprocess guard below raises the cost of an escape
    but does not prevent one — see the module docstring and EDI_PYTHON_ANALYSIS_ENABLED.
    """
    from backend.core.config import settings

    if not settings.python_analysis_enabled:
        raise SandboxDisabled(
            "Python analysis is disabled. It executes model-authored code, and the in-process "
            "guard is not a security boundary. Set EDI_PYTHON_ANALYSIS_ENABLED=true only when "
            "this process runs inside an isolated container.")
    timeout = timeout or settings.python_analysis_timeout_seconds
    with tempfile.TemporaryDirectory() as d:
        runner = Path(d) / "runner.py"
        runner.write_text(RUNNER, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-s", "-S", "-B", str(runner)],
                input=json.dumps({"code": code, "data": data}, default=str),
                capture_output=True, text=True, timeout=timeout, cwd=d,
                env={"PYTHONPATH": _site_packages(), "PATH": "", "SYSTEMROOT": _sysroot()},
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"analysis exceeded {timeout}s timeout"}
    if proc.returncode != 0 and not proc.stdout:
        return {"ok": False, "error": (proc.stderr or "sandbox crashed")[-1000:]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"unparseable sandbox output: {proc.stdout[:300]} {proc.stderr[-300:]}"}


def _site_packages() -> str:
    import sysconfig

    return sysconfig.get_paths()["purelib"]


def _sysroot() -> str:
    import os

    return os.environ.get("SYSTEMROOT", "")
