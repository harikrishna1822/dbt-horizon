from pathlib import Path

import yaml

from dbt_horizon.config import SnowflakeConfig
from dbt_horizon.rules.hooks import SNOWFLAKE_STATEMENT
from dbt_horizon.transform import Overlay
from dbt_horizon.transform.profile import READER_PAT_ENV, read_profile
from dbt_horizon.transform.project import (
    COMPAT_HOOK, patch_project_hooks, rename_macro, rewrite_source_locations, run_hooks_shim,
    table_alias_materialization,
)

SF = SnowflakeConfig.model_validate({"account": "org-acct", "reader": {"role": "READER", "pat_env": "MY_PAT"}})


def test_read_profile_has_no_secret_values(tmp_path):
    profile = read_profile("proj", SF, ["RAW", "LAKE"], tmp_path / "proj_local.duckdb", "MARTS")
    out = profile["proj"]["outputs"]["horizon_read"]
    assert profile["proj"]["target"] == "horizon_read"
    assert out["schema"] == "MARTS" and out["path"].endswith("proj_local.duckdb")
    assert out["secrets"][0]["client_secret"] == "{{ env_var('" + READER_PAT_ENV + "') }}"
    assert out["secrets"][0]["oauth2_scope"] == "session:role:READER"
    assert [(a["alias"], a["read_only"], a["options"]["access_delegation_mode"]) for a in out["attach"]] == [
        ("RAW", True, "vended_credentials"), ("LAKE", True, "vended_credentials")]


def test_read_profile_local_catalogs(tmp_path):
    out = read_profile("p", SF, ["RAW"], tmp_path / "o.duckdb", "S", local_catalog_dir=tmp_path)["p"]["outputs"]["horizon_read"]
    assert "secrets" not in out and "extensions" not in out
    assert out["attach"] == [{"path": str(tmp_path / "RAW.duckdb"), "alias": "RAW", "read_only": True}]


def project(tmp_path: Path, data: dict) -> Path:
    (tmp_path / "dbt_project.yml").write_text(yaml.safe_dump(data))
    return tmp_path


def test_patch_project_hooks(tmp_path):
    p = project(tmp_path, {"name": "x", "on-run-start": "grant usage on schema s to role r",
                           "on-run-end": ["grant x", "{{ log('done') }}"]})
    notes = patch_project_hooks(p, disabled=["grant usage on schema s to role r", "grant x"])
    data = yaml.safe_load((p / "dbt_project.yml").read_text())
    assert data["on-run-start"] == [COMPAT_HOOK]
    assert data["on-run-end"] == ["{{ log('done') }}"]
    assert len(notes) == 2


def test_rename_macro_only_definitions(tmp_path):
    p = project(tmp_path, {"name": "x", "macro-paths": ["macros", "more"]})
    (p / "macros").mkdir()
    (p / "more").mkdir()
    (p / "macros" / "db.sql").write_text(
        "{%- macro generate_database_name(custom, node) -%}{{ target.database }}{%- endmacro %}\n"
        "{% macro other() %}{{ generate_database_name(none) }}{% endmacro %}\n")
    (p / "more" / "untouched.sql").write_text("{% macro generate_database_name_helper() %}{% endmacro %}\n")
    assert rename_macro(p, "generate_database_name", "project_generate_database_name") == ["macros/db.sql"]
    text = (p / "macros" / "db.sql").read_text()
    assert "macro project_generate_database_name(custom" in text
    assert "{{ generate_database_name(none) }}" in text       # calls are left alone
    assert "generate_database_name_helper" in (p / "more" / "untouched.sql").read_text()


def test_rewrite_source_locations(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "src.yml").write_text(yaml.safe_dump({"version": 2, "sources": [
        {"name": "raw", "database": "RAW", "schema": "CORE", "tables": [{"name": "a"}]},
        {"name": "other", "schema": "OPS", "tables": [{"name": "b"}]}]}))
    rewrite_source_locations(tmp_path, "models/src.yml", "raw", "LAKE", "CORE_ICEBERG")
    data = yaml.safe_load((tmp_path / "models" / "src.yml").read_text())
    assert (data["sources"][0]["database"], data["sources"][0]["schema"]) == ("LAKE", "CORE_ICEBERG")
    assert data["sources"][1] == {"name": "other", "schema": "OPS", "tables": [{"name": "b"}]}


def test_run_hooks_shim_embeds_the_python_pattern():
    text = run_hooks_shim()
    assert "__SNOWFLAKE_STATEMENT__" not in text
    assert SNOWFLAKE_STATEMENT.pattern in text
    assert "{% macro run_hooks(hooks, inside_transaction=True) %}" in text


def test_table_alias_materialization_from_pinned_adapter():
    text = table_alias_materialization("dynamic_table")
    assert '{% materialization dynamic_table, adapter="duckdb"' in text
    assert "materialization table," not in text


def test_overlay_args(tmp_path):
    overlay = Overlay(mode="read", target="horizon_read", profiles_dir=tmp_path, output_path=None,
                      vars={"b": "2", "a": "1"}, exclude=["x+"], env_from={})
    args = overlay.dbt_args(tmp_path / "proj")
    assert args[args.index("--vars") + 1] == '{"a": "1", "b": "2"}'
    assert args[-2:] == ["--exclude", "x+"]
