"""Patches to the workspace copy of the dbt project: shim macros, project hooks, sources yml."""
from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

import yaml

from ..rules.hooks import SNOWFLAKE_STATEMENT

SHIM_DIR = "dbt_horizon"            # folder inside the project's first macro path
COMPAT_HOOK = "{{ dbt_horizon_install_compat() }}"


def shim_text(name: str) -> str:
    return resources.files("dbt_horizon").joinpath("shim", name).read_text(encoding="utf-8")


def read_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    return data if isinstance(data, dict) else {}


def write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def macro_dir(project: Path) -> Path:
    paths = read_yaml(project / "dbt_project.yml").get("macro-paths") or ["macros"]
    return project / paths[0] / SHIM_DIR


def rename_macro(project: Path, name: str, new_name: str) -> list[str]:
    """Rename a project macro definition (all macro paths). Returns the changed files."""
    paths = read_yaml(project / "dbt_project.yml").get("macro-paths") or ["macros"]
    pattern = re.compile(rf"(\{{%-?\s*macro\s+){re.escape(name)}(\s*\()")
    changed = []
    for base in paths:
        for path in sorted((project / base).rglob("*.sql")):
            if SHIM_DIR in path.relative_to(project).parts:
                continue
            text = path.read_text(encoding="utf-8")
            new = pattern.sub(rf"\g<1>{new_name}\g<2>", text)
            if new != text:
                path.write_text(new, encoding="utf-8")
                changed.append(path.relative_to(project).as_posix())
    return changed


def write_shim(project: Path, files: dict[str, str]) -> list[str]:
    target = macro_dir(project)
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in files.items():
        (target / name).write_text(text, encoding="utf-8")
        written.append((target / name).relative_to(project).as_posix())
    return written


def run_hooks_shim() -> str:
    return shim_text("run_hooks.sql").replace("__SNOWFLAKE_STATEMENT__", SNOWFLAKE_STATEMENT.pattern)


def table_alias_materialization(name: str) -> str:
    """dbt-duckdb's own table materialization under another name (e.g. dynamic_table), so
    Snowflake-only materializations build as plain tables. Copied from the pinned adapter."""
    source = resources.files("dbt.include.duckdb").joinpath("macros", "materializations", "table.sql")
    text = source.read_text(encoding="utf-8")
    header = re.compile(r"\{%\s*materialization\s+table\s*,\s*adapter\s*=\s*[\"']duckdb[\"']")
    if not header.search(text):
        raise RuntimeError("dbt-duckdb table materialization header not found: adapter version changed?")
    return (f"{{#- dbt-horizon shim: {name} built as a table (copy of dbt-duckdb's table materialization) -#}}\n"
            + header.sub(f'{{% materialization {name}, adapter="duckdb"', text, count=1))


def patch_project_hooks(project: Path, disabled: list[str]) -> list[str]:
    """Prepend the compat-macro hook to on-run-start and drop disabled project hooks."""
    path = project / "dbt_project.yml"
    data = read_yaml(path)
    notes = []
    for key in ("on-run-start", "on-run-end"):
        hooks = data.get(key) or []
        hooks = [hooks] if isinstance(hooks, str) else list(hooks)
        kept = [h for h in hooks if h not in disabled]
        notes += [f"{key}: disabled `{' '.join(h.split())}`" for h in hooks if h in disabled]
        if key == "on-run-start":
            kept = [COMPAT_HOOK, *kept]
        if kept:
            data[key] = kept
        else:
            data.pop(key, None)
    write_yaml(path, data)
    return notes


def rewrite_source_locations(project: Path, file: str, source_name: str,
                             database: str | None, schema: str) -> None:
    """Point one source (all its tables) at its mapped database/schema."""
    path = project / file
    data = read_yaml(path)
    for entry in data.get("sources", []) or []:
        if isinstance(entry, dict) and entry.get("name") == source_name:
            if database:
                entry["database"] = database
            entry["schema"] = schema
    write_yaml(path, data)
