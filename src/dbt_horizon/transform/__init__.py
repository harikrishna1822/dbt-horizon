"""Overlay: turn the analysis into files in the workspace copy plus dbt arguments.

Deterministic: same workspace + inventory + analysis -> same files and arguments.
Everything changed is listed in .dbt_horizon/overlay.json.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import AppConfig
from ..errors import DbtHorizonError
from ..inventory import Inventory
from ..repo import repo_slug
from ..rules import Analysis
from ..rules.materializations import TO_TABLE
from ..workspace import Workspace
from . import profile as profiles
from .project import (
    patch_project_hooks, rename_macro, rewrite_source_locations, run_hooks_shim,
    shim_text, table_alias_materialization, write_shim,
)


class TransformError(DbtHorizonError):
    pass


@dataclass
class Overlay:
    mode: str
    target: str
    profiles_dir: Path
    output_path: Path | None                       # read mode: local DuckDB file
    vars: dict[str, str]
    exclude: list[str]                             # dbt selectors
    env_from: dict[str, str]                       # env var for dbt -> config env var holding the value
    changes: list[str] = field(default_factory=list)

    def dbt_args(self, project_path: Path) -> list[str]:
        args = ["--project-dir", str(project_path), "--profiles-dir", str(self.profiles_dir),
                "--target", self.target]
        if self.vars:
            args += ["--vars", json.dumps(self.vars, sort_keys=True)]
        if self.exclude:
            args += ["--exclude", *self.exclude]
        return args


def output_path(cfg: AppConfig, ws: Workspace, inv: Inventory) -> Path:
    """Stable location (outside the per-run workspace). The file stem becomes the dbt
    database name, so it must not collide with an attached Snowflake database."""
    return cfg.workspace_root / "outputs" / repo_slug(ws.checkout.url) / f"{inv.project_name}_local.duckdb"


def exclude_selectors(inv: Inventory, analysis: Analysis) -> list[str]:
    """dbt selectors for every blocked subject and everything downstream of it (`+`)."""
    selectors = []
    sources = {s.unique_id: s for s in inv.sources}
    for uid in analysis.blocked:
        if uid in sources:
            selectors.append(f"source:{sources[uid].source_name}.{sources[uid].name}+")
        elif (node := inv.node(uid)) is not None:
            selectors.append(f"{node.name}+")
    return sorted(set(selectors))


def source_databases(analysis: Analysis) -> list[str]:
    return sorted({p.database for p in analysis.sources.values() if p.database})


def build_read_overlay(cfg: AppConfig, ws: Workspace, inv: Inventory, analysis: Analysis,
                       local_catalog_dir: Path | None = None) -> Overlay:
    """`local_catalog_dir`: test hook, see transform.profile.read_profile."""
    if analysis.mode != "read":
        raise TransformError(f"build_read_overlay needs a read-mode analysis, got {analysis.mode}")
    if not analysis.runnable:
        raise TransformError("The analysis has blocking findings and policy 'fail': nothing to run")
    project = ws.project_path
    changes: list[str] = []

    out = output_path(cfg, ws, inv)
    databases = source_databases(analysis)
    if out.stem.upper() in {d.upper() for d in databases}:
        raise TransformError(f"Output database name {out.stem} collides with an attached Snowflake database")
    out.parent.mkdir(parents=True, exist_ok=True)

    # Shim macros. Root-project macros win over dbt's and the adapter's, so no dispatch config.
    for name in ("generate_database_name", "run_hooks"):
        for file in rename_macro(project, name, f"project_{name}"):
            changes.append(f"{file}: macro {name} renamed to project_{name}")
    shim = {
        "run_hooks.sql": run_hooks_shim(),
        "compat.sql": shim_text("compat.sql"),
        "naming.sql": shim_text("naming_read.sql"),
    }
    used = {n.materialized for n in inv.of_type("model")
            if n.unique_id not in analysis.excluded and n.materialized in TO_TABLE}
    for name in sorted(used):
        shim[f"materialization_{name}.sql"] = table_alias_materialization(name)
    changes += [f"{f}: shim" for f in write_shim(project, shim)]

    # Project hooks: compat macros first; Snowflake administration statements dropped.
    disabled = [h["sql"] for h in analysis.disabled_hooks if h["subject"] == "project"]
    changes += [f"dbt_project.yml: {n}" for n in patch_project_hooks(project, disabled)]
    changes.append("dbt_project.yml: on-run-start installs DuckDB compat macros")

    # Sources whose location is not var-driven: rewrite their yml.
    rewrites = [f for f in analysis.findings if f.rule == "sources.rewrite_yml"]
    by_id = {s.unique_id: s for s in inv.sources}
    done = set()
    for finding in rewrites:
        src, plan = by_id[finding.subject], analysis.sources[finding.subject]
        if (src.file, src.source_name) in done:
            continue
        rewrite_source_locations(project, src.file, src.source_name, plan.database, plan.schema)
        done.add((src.file, src.source_name))
        changes.append(f"{src.file}: source {src.source_name} -> {plan.database}.{plan.schema}")

    profiles_dir = ws.metadata_path / "profiles"
    profiles.write_profiles(profiles_dir, profiles.read_profile(
        inv.profile_name, cfg.snowflake, databases, out, inv.target_schema or "main",
        local_catalog_dir=local_catalog_dir))
    changes.append(f"profiles.yml: target {profiles.READ_TARGET}, attaches {', '.join(databases)} read-only")

    env_from = {} if local_catalog_dir else {profiles.READER_PAT_ENV: cfg.snowflake.reader.pat_env}
    overlay = Overlay(
        mode="read", target=profiles.READ_TARGET, profiles_dir=profiles_dir, output_path=out,
        vars=dict(sorted(analysis.var_overrides.items())), exclude=exclude_selectors(inv, analysis),
        env_from=env_from, changes=changes)
    _write_overlay_json(ws, overlay)
    return overlay


def _write_overlay_json(ws: Workspace, overlay: Overlay) -> None:
    (ws.metadata_path / "overlay.json").write_text(json.dumps({
        "mode": overlay.mode, "target": overlay.target,
        "output": str(overlay.output_path) if overlay.output_path else None,
        "vars": overlay.vars, "exclude": overlay.exclude,
        "env": sorted(overlay.env_from), "changes": overlay.changes,
    }, indent=2) + "\n", encoding="utf-8")

