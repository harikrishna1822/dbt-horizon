"""Run dbt in a subprocess.

A subprocess, not dbtRunner in-process: the env (PATs) is passed per call and
dies with it, no dbt global state carries over between runs, and no file
handles (logs/dbt.log) stay open on the workspace, which Windows would lock.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field

from .errors import DbtHorizonError

# Same interpreter as the app, so the pinned dbt-core / dbt-duckdb are used.
_DBT = [sys.executable, "-c", "import sys; from dbt.cli.main import cli; sys.exit(cli())"]


class DbtError(DbtHorizonError):
    pass


@dataclass
class DbtResult:
    returncode: int
    errors: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.returncode == 0


def run_dbt(args: list[str], env: Mapping[str, str], check: bool = True) -> DbtResult:
    """`args` without the leading 'dbt'. Logs are read as JSON lines from stdout."""
    cmd = [*_DBT, args[0], "--log-format", "json", "--no-send-anonymous-usage-stats", *args[1:]]
    proc = subprocess.run(cmd, env=dict(env), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    result = DbtResult(proc.returncode)
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        result.events.append(event)
        info = event.get("info", {})
        if info.get("level") == "error" and info.get("msg"):
            result.errors.append(info["msg"])
    if check and not result.success:
        detail = "\n".join(result.errors) or proc.stderr.strip() or proc.stdout.strip()[-2000:]
        raise DbtError(f"dbt {args[0]} failed:\n{detail}")
    return result
