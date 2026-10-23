"""Against the real account. Opt in: DBT_HORIZON_ONLINE=1 and DBT_HORIZON_ENV_FILE=<env file with the PATs>."""
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dbt_horizon.cli import app

EXAMPLE = Path(__file__).parents[2] / "examples" / "snowflake-dbt.yml"

pytestmark = [
    pytest.mark.online,
    pytest.mark.skipif(os.getenv("DBT_HORIZON_ONLINE") != "1", reason="set DBT_HORIZON_ONLINE=1"),
]


def invoke(*args: str):
    args = [*args, "-c", str(EXAMPLE)]
    if os.getenv("DBT_HORIZON_ENV_FILE"):
        args += ["--env-file", os.environ["DBT_HORIZON_ENV_FILE"]]
    return CliRunner().invoke(app, args)


def report(mode: str) -> dict:
    out = invoke("analyze", "--mode", mode)
    assert out.exit_code == 0, out.output
    json_path = out.output.strip().splitlines()[-1].strip()
    return json.loads(Path(json_path).read_text())


def test_connect_snowflake_dbt():
    result = invoke("connect", "--probe")
    assert result.exit_code == 0, result.output
    for table in ["DIM_STORE", "DIM_PRODUCT", "FACT_SALES"]:
        assert f"    {table}  OK" in result.output
    assert "Connection OK." in result.output


def test_analyze_read_matches_poc():
    data = report("read")
    assert data["runnable"] and data["excluded"] == []
    assert {s["reads"] for s in data["sources"]} == {
        f"RETAIL_ANALYTICS.CORE_ICEBERG_S3.{t}" for t in ("DIM_PRODUCT", "DIM_STORE", "FACT_SALES")}
    assert all(s["verified"] for s in data["sources"])
    assert data["var_overrides"] == {"source_schema": "CORE_ICEBERG_S3"}
    assert data["counts"] == {"ok": 0, "auto_fixed": 1, "review": 0, "needs_mapping": 0, "unsupported": 0}


def test_analyze_write_matches_poc():
    data = report("write")
    planned = {n["name"]: (n["planned"], n["target"]) for n in data["nodes"]}
    assert planned["stg_sales"] == ("ephemeral", None)
    assert planned["fct_sales"] == ("iceberg_table", "RETAIL_ANALYTICS.DBT_RETAIL_ICEBERG.FCT_SALES")
    assert {f["rule"] for f in data["findings"]} == {
        "materialization.iceberg_table", "materialization.view_to_ephemeral",
        "macros.naming_override", "sources.var_override"}
