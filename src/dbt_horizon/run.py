"""Run dbt build in the workspace with the overlay, and summarize run_results.json."""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

from .config import resolve_secret
from .dbt_cli import DbtResult, run_dbt
from .inventory import project_packages
from .transform import Overlay
from .workspace import Workspace

PASSED = {"success", "pass"}


@dataclass(frozen=True)
class NodeResult:
    unique_id: str
    status: str
    message: str
    seconds: float
    rows: int | None = None


@dataclass
class RunSummary:
    returncode: int
    results: list[NodeResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)       # dbt errors outside of nodes

    @property
    def success(self) -> bool:
        return self.returncode == 0 and all(r.status in PASSED | {"skipped"} for r in self.results)

    def counts(self) -> dict[str, int]:
        return dict(sorted(Counter(r.status for r in self.results).items()))

    def failed(self) -> list[NodeResult]:
        return [r for r in self.results if r.status not in PASSED | {"skipped"}]

    def to_dict(self) -> dict:
        return {"returncode": self.returncode, "success": self.success, "counts": self.counts(),
                "errors": self.errors,
                "results": [r.__dict__ for r in self.results]}


def dbt_env(base: Mapping[str, str], overlay: Overlay) -> dict[str, str]:
    """`base` plus the overlay's env vars (e.g. DBT_HORIZON_READER_PAT), resolved from the
    config's env var names. Only this dict reaches the dbt subprocess."""
    env = dict(base)
    for name, source in overlay.env_from.items():
        env[name] = resolve_secret(source, base, f"for {name}")
    return env


def read_results(ws: Workspace) -> list[NodeResult]:
    path = ws.project_path / "target" / "run_results.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    results = []
    for r in data.get("results", []):
        rows = (r.get("adapter_response") or {}).get("rows_affected")
        results.append(NodeResult(r["unique_id"], r["status"], (r.get("message") or "").strip(),
                                  round(r.get("execution_time") or 0.0, 2), rows))
    return sorted(results, key=lambda r: r.unique_id)


def run_build(ws: Workspace, overlay: Overlay, base_env: Mapping[str, str],
              select: list[str] | None = None) -> RunSummary:
    env = dbt_env(base_env, overlay)
    common = overlay.dbt_args(ws.project_path)
    if project_packages(ws.project_path):
        run_dbt(["deps", "--project-dir", str(ws.project_path), "--profiles-dir", str(overlay.profiles_dir),
                 "--target", overlay.target, "--quiet"], env)
    args = ["build", *common]
    if select:
        args += ["--select", *select]
    result: DbtResult = run_dbt(args, env, check=False)
    summary = RunSummary(result.returncode, read_results(ws))
    if not summary.results and result.returncode != 0:
        summary.errors = result.errors or ["dbt build failed before running any node"]
    (ws.metadata_path / f"run_{overlay.mode}.json").write_text(
        json.dumps(summary.to_dict(), indent=2) + "\n", encoding="utf-8")
    return summary
