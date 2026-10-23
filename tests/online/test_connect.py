"""Against the real account. Opt in: DBT_HORIZON_ONLINE=1 and DBT_HORIZON_ENV_FILE=<env file with the PATs>."""
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


def test_connect_snowflake_dbt():
    args = ["connect", "-c", str(EXAMPLE), "--probe"]
    if os.getenv("DBT_HORIZON_ENV_FILE"):
        args += ["--env-file", os.environ["DBT_HORIZON_ENV_FILE"]]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    for table in ["DIM_STORE", "DIM_PRODUCT", "FACT_SALES"]:
        assert f"    {table}  OK" in result.output
    assert "Connection OK." in result.output
