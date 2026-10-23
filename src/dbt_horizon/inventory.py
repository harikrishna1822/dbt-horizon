"""Inventory of a dbt project, from dbt's own parser.

`dbt parse` runs in the workspace against a throwaway DuckDB profile (no
Snowflake credentials needed); everything else is read from manifest.json, so
the inventory sees exactly what dbt sees: rendered configs, refs, hooks, macros.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .dbt_cli import DbtError, run_dbt
from .errors import DbtHorizonError
from .workspace import Workspace

PARSE_TARGET = "dbt_horizon_parse"
OVERRIDABLE_MACROS = ("generate_schema_name", "generate_alias_name", "generate_database_name")
_VAR_CALL = re.compile(r"""\bvar\(\s*['"]([A-Za-z0-9_]+)['"]""")


class InspectError(DbtHorizonError):
    pass


@dataclass(frozen=True)
class SourceInfo:
    unique_id: str
    source_name: str
    name: str
    database: str | None
    schema: str
    identifier: str
    file: str                                  # project-relative yml path
    # field ("database" / "schema") -> var name, when the yml sets it with var('...')
    vars: Mapping[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        return ".".join(p for p in (self.database, self.schema, self.identifier) if p)


@dataclass(frozen=True)
class NodeInfo:
    unique_id: str
    name: str
    resource_type: str                         # model | seed | snapshot | test | operation
    package: str
    file: str
    language: str = "sql"
    materialized: str | None = None
    database: str | None = None
    schema: str | None = None
    alias: str | None = None
    config: Mapping[str, object] = field(default_factory=dict)
    raw_code: str = ""
    depends_on: tuple[str, ...] = ()
    pre_hooks: tuple[str, ...] = ()
    post_hooks: tuple[str, ...] = ()


@dataclass
class Inventory:
    project_name: str
    profile_name: str
    dbt_version: str
    sources: list[SourceInfo]
    nodes: list[NodeInfo]
    project_hooks: dict[str, list[str]]        # on-run-start / on-run-end -> SQL
    root_macro_overrides: list[str]            # e.g. generate_schema_name defined in the project
    root_materializations: list[str]           # materializations defined in the project
    packages: list[dict]                       # entries of packages.yml / dependencies.yml
    child_map: dict[str, list[str]]
    project_vars: dict[str, object]
    target_database: str | None = None         # Snowflake target the names were resolved for
    target_schema: str | None = None

    def of_type(self, *types: str) -> list[NodeInfo]:
        return [n for n in self.nodes if n.resource_type in types and n.package == self.project_name]

    def node(self, unique_id: str) -> NodeInfo | None:
        return next((n for n in self.nodes if n.unique_id == unique_id), None)


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as exc:
        raise InspectError(f"{path.name} is not valid YAML: {exc}") from None
    return data if isinstance(data, dict) else {}


def original_target(project: Path, profile_name: str) -> tuple[str | None, str | None]:
    """(database, schema) of the default target in the repo's own profiles.yml, if it has
    literal values. Jinja / env_var() values can't be known here: config snowflake.target."""
    profile = _read_yaml(project / "profiles.yml").get(profile_name)
    if not isinstance(profile, dict):
        return None, None
    output = (profile.get("outputs") or {}).get(profile.get("target"))
    if not isinstance(output, dict):
        return None, None
    literal = lambda v: str(v) if v and "{{" not in str(v) else None
    return literal(output.get("database")), literal(output.get("schema"))


def write_parse_profile(ws: Workspace, profile_name: str, database: str, schema: str) -> Path:
    """DuckDB profile that resolves names like the Snowflake target: the in-memory database is
    attached under the Snowflake database name, so model/source database and schema come out
    as in Snowflake (dbt-duckdb accepts a database that matches an attach alias)."""
    profiles_dir = ws.metadata_path / "parse_profile"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / "profiles.yml").write_text(yaml.safe_dump({
        profile_name: {"target": PARSE_TARGET, "outputs": {PARSE_TARGET: {
            "type": "duckdb", "path": ":memory:", "database": database, "schema": schema, "threads": 1,
            "attach": [{"path": ":memory:", "alias": database}]}}},
    }, sort_keys=False), encoding="utf-8")
    return profiles_dir


def _invoke_dbt(args: list[str], env: Mapping[str, str] | None) -> None:
    try:
        run_dbt(args, env if env is not None else os.environ)
    except DbtError as exc:
        raise InspectError(str(exc)) from None


def parse_project(ws: Workspace, env: Mapping[str, str] | None = None,
                  target: tuple[str | None, str | None] = (None, None)) -> Inventory:
    """`target`: (database, schema) of the Snowflake target from config; missing parts are
    taken from the repo's profiles.yml."""
    project = ws.project_path
    meta = _read_yaml(project / "dbt_project.yml")
    if not meta.get("name"):
        raise InspectError(f"{project / 'dbt_project.yml'} has no name")
    profile_name = str(meta.get("profile") or meta["name"])
    repo_db, repo_schema = original_target(project, profile_name)
    database, schema = target[0] or repo_db, target[1] or repo_schema
    if not database or not schema:
        raise InspectError(
            "The Snowflake target's database/schema are not literal values in the repo's profiles.yml: "
            "set snowflake.target.database and snowflake.target.schema in the config")
    profiles_dir = write_parse_profile(ws, profile_name, database, schema)
    common = ["--project-dir", str(project), "--profiles-dir", str(profiles_dir),
              "--target", PARSE_TARGET, "--quiet"]
    if _packages(project):
        _invoke_dbt(["deps", *common], env)
    _invoke_dbt(["parse", "--no-partial-parse", *common], env)
    manifest_path = project / "target" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    inv = build_inventory(manifest, project, profile_name)
    inv.target_database, inv.target_schema = database, schema
    return inv


def _packages(project: Path) -> list[dict]:
    found: list[dict] = []
    for name in ("packages.yml", "dependencies.yml"):
        found += [p for p in _read_yaml(project / name).get("packages", []) or [] if isinstance(p, dict)]
    return found


def _source_vars(project: Path, source: dict, cache: dict[str, dict]) -> dict[str, str]:
    """Which of database/schema the yml sets through var('...'), table level before source level."""
    rel = source["original_file_path"]
    if rel not in cache:
        cache[rel] = _read_yaml(project / rel)
    entry = next((s for s in cache[rel].get("sources", []) or []
                  if isinstance(s, dict) and s.get("name") == source["source_name"]), None)
    if entry is None:
        return {}
    table = next((t for t in entry.get("tables", []) or []
                  if isinstance(t, dict) and t.get("name") == source["name"]), {})
    found = {}
    for key in ("database", "schema"):
        raw = table.get(key, entry.get(key))
        match = _VAR_CALL.search(raw) if isinstance(raw, str) else None
        if match:
            found[key] = match.group(1)
    return found


def snowflake_name(name: str | None, quoted: bool | None) -> str | None:
    """The name Snowflake resolves: unquoted identifiers are upper-cased (dbt-snowflake
    doesn't quote source names by default), quoted ones are exact."""
    if name is None or quoted:
        return name
    return name.upper()


def build_inventory(manifest: dict, project: Path, profile_name: str) -> Inventory:
    """Pure function of manifest.json (+ the yml files for var detection): unit-testable."""
    meta = manifest["metadata"]
    project_name = meta.get("project_name") or _read_yaml(project / "dbt_project.yml").get("name", "")
    yml_cache: dict[str, dict] = {}

    sources = []
    for uid, s in sorted(manifest.get("sources", {}).items()):
        if s.get("package_name") != project_name:
            continue
        quoting = s.get("quoting") or {}
        sources.append(SourceInfo(
            unique_id=uid, source_name=s["source_name"], name=s["name"],
            database=snowflake_name(s.get("database"), quoting.get("database")),
            schema=snowflake_name(s["schema"], quoting.get("schema")),
            identifier=snowflake_name(s.get("identifier") or s["name"], quoting.get("identifier")),
            file=s["original_file_path"], vars=_source_vars(project, s, yml_cache)))

    nodes = []
    for uid, n in sorted(manifest.get("nodes", {}).items()):
        cfg = n.get("config", {}) or {}
        nodes.append(NodeInfo(
            unique_id=uid, name=n["name"], resource_type=n["resource_type"], package=n["package_name"],
            file=n.get("original_file_path", ""), language=n.get("language") or "sql",
            materialized=cfg.get("materialized"), database=n.get("database"), schema=n.get("schema"),
            alias=n.get("alias"), config=cfg, raw_code=n.get("raw_code") or "",
            depends_on=tuple(n.get("depends_on", {}).get("nodes", [])),
            pre_hooks=tuple(h.get("sql", "") for h in cfg.get("pre-hook", []) or []),
            post_hooks=tuple(h.get("sql", "") for h in cfg.get("post-hook", []) or []),
        ))

    hooks: dict[str, list[str]] = {"on-run-start": [], "on-run-end": []}
    for n in nodes:
        if n.resource_type == "operation" and n.package == project_name:
            for kind in hooks:
                if kind in n.name:
                    hooks[kind].append(n.raw_code)

    macros = [m for m in manifest.get("macros", {}).values() if m.get("package_name") == project_name]
    project_yml = _read_yaml(project / "dbt_project.yml")
    return Inventory(
        project_name=project_name,
        profile_name=profile_name,
        dbt_version=meta.get("dbt_version", ""),
        sources=sources,
        nodes=nodes,
        project_hooks=hooks,
        root_macro_overrides=sorted(m["name"] for m in macros if m["name"] in OVERRIDABLE_MACROS),
        root_materializations=sorted(m["name"].removeprefix("materialization_")
                                     for m in macros if m["name"].startswith("materialization_")),
        packages=_packages(project),
        child_map={k: list(v) for k, v in manifest.get("child_map", {}).items()},
        project_vars=project_yml.get("vars", {}) or {},
    )
