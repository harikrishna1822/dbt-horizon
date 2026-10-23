"""Parse the sf_features fixture with real dbt (offline) and check every rule family."""
import json
import shutil
from pathlib import Path

import pytest

from dbt_horizon.config import AppConfig
from dbt_horizon.inventory import parse_project
from dbt_horizon.report import report_markdown, write_report
from dbt_horizon.rules import Outcome, analyze
from tests.conftest import fixture_workspace

# What Horizon "lists": the Iceberg copies of RAW.SALES, nothing for RAW.EVENTS.
CATALOG = {"RAW.SALES_ICEBERG.ORDERS", "RAW.SALES_ICEBERG.CUSTOMERS"}


def config(**overrides) -> AppConfig:
    return AppConfig.model_validate({
        "repo": {"url": "file:///unused"},
        "snowflake": {"account": "myorg-myaccount",
                      "reader": {"role": "R", "pat_env": "SNOWFLAKE_PAT"},
                      "writer": {"role": "W", "pat_env": "SNOWFLAKE_WRITE_PAT"}},
        "source_mapping": [{"match": {"database": "RAW", "schema": "SALES"}, "to": {"schema": "SALES_ICEBERG"}}],
        **overrides,
    })


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    ws = fixture_workspace(tmp_path_factory.mktemp("analyze"))
    return ws, parse_project(ws)


def test_parse_error_is_reported(tmp_path, parsed):
    ws, _ = parsed
    broken = tmp_path / "broken"
    shutil.copytree(ws.project_path, broken)
    (broken / "models" / "bad.sql").write_text("select * from {{ ref('does_not_exist') }}\n")
    from dbt_horizon.inventory import InspectError
    from dbt_horizon.workspace import Workspace
    with pytest.raises(InspectError, match="does_not_exist"):
        parse_project(Workspace(root=tmp_path, project_dir="broken", checkout=ws.checkout))


def by_name(analysis, inv):
    return {n.name: analysis.worst(n.unique_id) for n in inv.of_type("model", "seed", "snapshot")}


def test_inventory(parsed):
    _, inv = parsed
    assert (inv.project_name, inv.target_database, inv.target_schema) == ("sf_features", "ANALYTICS", "MARTS")
    sources = {s.name: s for s in inv.sources}
    assert sources["orders"].vars == {"database": "raw_db", "schema": "raw_schema"}
    assert sources["clicks"].vars == {}
    assert str(sources["orders"]) == "RAW.SALES.ORDERS"
    models = {n.name: n for n in inv.of_type("model")}
    assert models["py_scores"].language == "python"
    assert models["clicks_daily"].post_hooks and "grant select" in models["clicks_daily"].post_hooks[0]
    assert models["stg_orders"].schema == "MARTS" and models["stg_orders"].database == "ANALYTICS"
    assert inv.root_macro_overrides == ["generate_schema_name"]
    assert len(inv.project_hooks["on-run-end"]) == 2
    assert [n.name for n in inv.of_type("seed")] == ["country_codes"]


def test_read_mode(parsed):
    _, inv = parsed
    analysis = analyze(inv, config(), CATALOG, mode="read")
    outcomes = by_name(analysis, inv)
    assert outcomes == {
        "clicks_daily": Outcome.AUTO_FIXED,      # cluster_by + grant hook; blocked via its source
        "country_codes": Outcome.OK,
        "dyn_counts": Outcome.AUTO_FIXED,        # dynamic_table -> table, DIV0, grant hook disabled
        "dyn_summary": Outcome.AUTO_FIXED,       # dynamic_table -> table, Snowflake configs stripped
        "hierarchy": Outcome.UNSUPPORTED,        # CONNECT BY
        "hierarchy_report": Outcome.OK,          # fine itself, excluded as downstream
        "orders_flat": Outcome.REVIEW,           # FLATTEN + col:field
        "pipe_load": Outcome.UNSUPPORTED,        # unknown materialization
        "py_scores": Outcome.UNSUPPORTED,        # Python model
        "stg_customers": Outcome.OK,             # iff(/flatten( only in a comment, 'key:value' a string
        "stg_orders": Outcome.AUTO_FIXED,        # IFF + NVL compat macros
    }
    clicks = next(s for s in inv.sources if s.name == "clicks")
    assert analysis.worst(clicks.unique_id) is Outcome.NEEDS_MAPPING
    assert analysis.var_overrides == {"raw_schema": "SALES_ICEBERG"}
    assert analysis.sources[clicks.unique_id].verified is False

    excluded = {uid.split(".")[-1] if uid.startswith("model.") else uid for uid in analysis.excluded}
    assert {"clicks_daily", "hierarchy", "hierarchy_report", "pipe_load", "py_scores"} <= excluded
    assert not {"stg_orders", "orders_flat", "dyn_summary"} & excluded
    assert any(uid.startswith("test.") and "hierarchy_report" in uid for uid in analysis.excluded)
    assert analysis.runnable

    hooks = [h["sql"] for h in analysis.disabled_hooks]
    assert any(h.startswith("grant usage") for h in hooks) and any(h.startswith("grant select") for h in hooks)
    assert analysis.stripped_configs[next(n.unique_id for n in inv.of_type("model") if n.name == "dyn_summary")] \
        == ["snowflake_warehouse", "target_lag"]


def test_write_mode(parsed):
    _, inv = parsed
    analysis = analyze(inv, config(mode="write"), CATALOG)
    planned = {inv.node(uid).name: m for uid, m in analysis.materializations.items()}
    assert planned["stg_orders"] == "ephemeral"
    assert planned["orders_flat"] == "iceberg_table"
    assert planned["dyn_summary"] == "iceberg_table"
    assert planned["clicks_daily"] == "iceberg_incremental"
    assert planned["country_codes"] == "seed_local"
    targets = {inv.node(uid).name: t for uid, t in analysis.targets.items()}
    assert targets["orders_flat"] == "ANALYTICS.MARTS_ICEBERG.ORDERS_FLAT"
    assert "stg_orders" not in targets                  # ephemeral: not persisted
    assert any(f.rule == "macros.naming_override" for f in analysis.findings)


def test_fail_policy_blocks_run(parsed):
    _, inv = parsed
    analysis = analyze(inv, config(unsupported_policy="fail"), CATALOG, mode="read")
    assert analysis.excluded == [] and analysis.blocked and not analysis.runnable


def test_offline_marks_sources_unverified(parsed):
    _, inv = parsed
    analysis = analyze(inv, config(), None, mode="read")
    assert all(p.verified is None for p in analysis.sources.values())
    assert all(analysis.worst(uid) is Outcome.REVIEW for uid in analysis.sources)


def test_deterministic_report(parsed):
    ws, inv = parsed
    first = write_report(ws, inv, analyze(inv, config(), CATALOG, mode="read"))
    texts = [p.read_text() for p in first]
    second = write_report(ws, inv, analyze(inv, config(), CATALOG, mode="read"))
    assert [p.read_text() for p in second] == texts
    data = json.loads(texts[0])
    assert data["counts"]["unsupported"] == 3 and data["counts"]["needs_mapping"] == 1
    assert "| clicks_daily | model | incremental | incremental | excluded |" in report_markdown(data)
