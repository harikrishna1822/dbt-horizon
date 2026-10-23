"""Read mode end to end, offline: the fixture project's sources live in a local DuckDB file
attached under the Snowflake database name (RAW) instead of Horizon; dbt build really runs."""
import json
import os

import duckdb
import yaml
import pytest

from dbt_horizon.inventory import parse_project
from dbt_horizon.rules import analyze
from dbt_horizon.run import run_build
from dbt_horizon.transform import build_read_overlay
from tests.conftest import fixture_workspace
from tests.offline.test_analyze_fixture import CATALOG, config


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("run_read")
    catalogs = root / "catalogs"
    catalogs.mkdir()
    con = duckdb.connect(str(catalogs / "RAW.duckdb"))
    con.execute("CREATE SCHEMA SALES_ICEBERG")
    con.execute("""CREATE TABLE SALES_ICEBERG.ORDERS AS SELECT * FROM (VALUES
        (1, 10.0, 'open',   [{'sku': 'a'}]),
        (2, -5.0, NULL,     [{'sku': 'b'}]),
        (3, 20.0, 'closed', [{'sku': 'c'}])) t(order_id, amount, status, items)""")
    con.execute("CREATE TABLE SALES_ICEBERG.CUSTOMERS AS SELECT * FROM (VALUES (1, NULL), (2, 1)) t(id, parent_id)")
    con.close()

    ws = fixture_workspace(root)
    inv = parse_project(ws)
    cfg = config(workspace_root=str(root / "cache"))
    analysis = analyze(inv, cfg, CATALOG, mode="read")
    overlay = build_read_overlay(cfg, ws, inv, analysis, local_catalog_dir=catalogs)
    summary = run_build(ws, overlay, base_env=dict(os.environ))
    return ws, overlay, summary


def status(summary, name: str) -> str:
    return next(r.status for r in summary.results if r.unique_id.split(".")[-1] == name
                and r.unique_id.startswith(("model.", "seed.")))


def test_overlay_excludes_blocked_and_downstream(built):
    _, overlay, _ = built
    assert overlay.vars == {"raw_schema": "SALES_ICEBERG"}
    assert overlay.exclude == ["hierarchy+", "pipe_load+", "py_scores+", "source:events.clicks+"]
    assert overlay.env_from == {}


def test_build_results(built):
    _, _, summary = built
    assert status(summary, "stg_orders") == "success"         # IFF + NVL via compat macros
    assert status(summary, "stg_customers") == "success"
    assert status(summary, "dyn_counts") == "success"         # dynamic_table -> table, grant hook skipped
    assert status(summary, "country_codes") == "success"
    assert status(summary, "orders_flat") == "error"          # FLATTEN: 'review', needs the transpile layer
    assert status(summary, "dyn_summary") == "skipped"        # downstream of the error
    ran = {r.unique_id.split(".")[-1] for r in summary.results}
    assert not {"hierarchy", "hierarchy_report", "py_scores", "pipe_load", "clicks_daily"} & ran
    assert any(r.unique_id.startswith("test.") and r.status == "pass" for r in summary.results)


def test_output_data(built):
    _, overlay, _ = built
    con = duckdb.connect(str(overlay.output_path), read_only=True)
    rows = con.execute('SELECT status, n, avg_amount FROM "MARTS"."dyn_counts" ORDER BY status').fetchall()
    # stg_orders: iff(amount > 0, amount, 0) turns -5.0 into 0; nvl(status, 'none'); dyn_counts: div0
    assert rows == [("closed", 1, 20.0), ("none", 1, 0.0), ("open", 1, 10.0)]
    con.close()


def test_project_hooks_patched(built):
    ws, _, _ = built
    project = yaml.safe_load((ws.project_path / "dbt_project.yml").read_text())
    assert project["on-run-start"] == ["{{ dbt_horizon_install_compat() }}"]
    assert project["on-run-end"] == ["{{ log('run finished', info=True) }}"]   # grant dropped
    overlay_json = json.loads((ws.metadata_path / "overlay.json").read_text())
    assert any("materialization_dynamic_table.sql" in c for c in overlay_json["changes"])
    # read mode keeps the project's generate_schema_name: no rename of it
    assert not any("generate_schema_name" in c for c in overlay_json["changes"])
