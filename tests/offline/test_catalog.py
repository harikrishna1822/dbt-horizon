import duckdb
import pytest

from dbt_horizon.catalog import HorizonCatalog, TableRef, explain
from dbt_horizon.config import SnowflakeConfig
from dbt_horizon.errors import CatalogError

SF = SnowflakeConfig.model_validate({
    "account": "myorg-myaccount",
    "reader": {"role": "DUCKDB_READER", "pat_env": "SNOWFLAKE_PAT"},
})


class RecordingConnection:
    """Captures SQL instead of talking to Horizon."""

    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))
        return self


@pytest.fixture
def local_db(tmp_path):
    """A local DuckDB file shaped like a Horizon database: schema CORE_ICEBERG_S3 with 2 tables."""
    path = tmp_path / "RETAIL_ANALYTICS.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA CORE_ICEBERG_S3")
    con.execute("CREATE TABLE CORE_ICEBERG_S3.DIM_STORE AS SELECT 1 AS STORE_SK")
    con.execute("CREATE TABLE CORE_ICEBERG_S3.FACT_SALES AS SELECT 1 AS SALE_SK, 1 AS STORE_SK")
    con.close()
    return path


def test_list_and_probe_local(local_db):
    catalog = HorizonCatalog(SF)
    alias = catalog.attach_local(local_db, "RETAIL_ANALYTICS")
    assert catalog.list_schemas(alias) == ["CORE_ICEBERG_S3"]
    tables = catalog.list_tables(alias)
    assert [str(t) for t in tables] == ["RETAIL_ANALYTICS.CORE_ICEBERG_S3.DIM_STORE",
                                        "RETAIL_ANALYTICS.CORE_ICEBERG_S3.FACT_SALES"]
    assert catalog.probe(tables[0]) is None
    error = catalog.probe(TableRef("RETAIL_ANALYTICS", "CORE_ICEBERG_S3", "MISSING"))
    assert error.startswith("Not found")
    # fully qualified SQL resolves unchanged because the alias is the database name
    assert catalog.con.execute("SELECT COUNT(*) FROM RETAIL_ANALYTICS.CORE_ICEBERG_S3.FACT_SALES").fetchone() == (1,)
    catalog.close()


def test_secret_and_attach_sql():
    con = RecordingConnection()
    catalog = HorizonCatalog(SF, con=con)
    catalog.add_secret("horizon_reader", "pa't", "DUCKDB_READER")
    catalog.attach("RETAIL_ANALYTICS", secret="horizon_reader")
    catalog.attach("RETAIL_ANALYTICS", secret="horizon_writer", alias="RETAIL_ANALYTICS__OUT", write=True)
    install, secret, reader, writer = con.statements
    assert "LOAD iceberg" in install
    assert "CLIENT_SECRET 'pa''t'" in secret          # quoted, not injectable
    assert "OAUTH2_SERVER_URI 'https://myorg-myaccount.snowflakecomputing.com/polaris/api/catalog/v1/oauth/tokens'" in secret
    assert "OAUTH2_SCOPE 'session:role:DUCKDB_READER'" in secret
    assert reader == ("ATTACH 'RETAIL_ANALYTICS' AS \"RETAIL_ANALYTICS\" (TYPE iceberg, SECRET \"horizon_reader\", "
                      "ENDPOINT 'https://myorg-myaccount.snowflakecomputing.com/polaris/api/catalog', "
                      "ACCESS_DELEGATION_MODE 'vended_credentials', READ_ONLY)")
    assert "READ_ONLY" not in writer
    for option in ["STAGE_CREATE_TABLES false", "DISABLE_MULTI_TABLE_COMMIT true",
                   "SKIP_CREATE_TABLE_METADATA_UPDATES true", "REMOVE_FILES_ON_DELETE false"]:
        assert option in writer
    assert catalog.attached == {"RETAIL_ANALYTICS": "RETAIL_ANALYTICS",
                                "RETAIL_ANALYTICS__OUT": "RETAIL_ANALYTICS"}


def test_builtin_catalog_names_refused():
    with pytest.raises(CatalogError, match="already uses that name"):
        HorizonCatalog(SF, con=RecordingConnection()).attach("MEMORY", secret="s")


@pytest.mark.parametrize("message, writing, starts", [
    ("HTTP 401 Unauthorized", False, "PAT rejected"),
    ("Conflict_409 ... AlreadyExistsException", True, "Name already taken"),
    ("namespaces returned Forbidden_403", False, "Schema not visible"),
    ("Forbidden_403 on PUT s3://bucket/x.parquet", True, "Write denied"),
    ("Forbidden_403 on GET s3://bucket/x.parquet", False, "S3 read denied"),
    ("Table with name X does not exist", False, "Not found"),
    ("something else entirely", False, "something else entirely"),
])
def test_explain(message, writing, starts):
    assert explain(Exception(message), writing=writing).startswith(starts)


def test_explain_ignores_codes_inside_paths():
    msg = "file s3://b/data/00401-abc.parquet missing"
    assert not explain(Exception(msg)).startswith("PAT rejected")
