"""DuckDB connection to Snowflake Horizon (Iceberg REST Catalog).

Each Snowflake database is attached under its own name, so fully qualified
DATABASE.SCHEMA.TABLE SQL resolves unchanged. The reader attach is READ_ONLY;
a writer attach uses the options Horizon needs for external-engine writes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import duckdb

from .config import SnowflakeConfig
from .errors import CatalogError

# Horizon rejects staged creates, multi-table commits, metadata updates right after
# a create, and file deletes on rollback; DuckDB >= 1.5.4 can turn each off.
HORIZON_WRITE_OPTIONS = {
    "STAGE_CREATE_TABLES": False,
    "DISABLE_MULTI_TABLE_COMMIT": True,
    "SKIP_CREATE_TABLE_METADATA_UPDATES": True,
    "REMOVE_FILES_ON_DELETE": False,
}

# Catalog names DuckDB already has; a Snowflake database can't be attached under them.
BUILTIN_CATALOGS = {"MEMORY", "SYSTEM", "TEMP"}


def sql_str(value: str) -> str:
    """Quote a string literal for DuckDB DDL (secrets and ATTACH can't use bind params)."""
    return "'" + value.replace("'", "''") + "'"


def sql_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@dataclass(frozen=True, order=True)
class TableRef:
    database: str
    schema: str
    name: str

    def sql(self) -> str:
        return ".".join(sql_ident(p) for p in (self.database, self.schema, self.name))

    def __str__(self) -> str:
        return f"{self.database}.{self.schema}.{self.name}"


def explain(exc: Exception, writing: bool = False) -> str:
    """Map common Horizon/S3 failures to their fix; unknown errors are returned as is."""
    msg = str(exc).strip()
    low = msg.lower()
    # HTTP code as its own token: Horizon writes "Conflict_409", S3 paths contain hex/UUIDs.
    code = lambda c: re.search(rf"(?<![0-9a-z]){c}(?![0-9a-z])", low)
    denied = code("403") or "access denied" in low or "forbidden" in low or "insufficient privileges" in low
    if "network policy" in low or "not allowed to access" in low:
        hint = "This machine's public IP is not allowed by the network policy on the PAT's user."
    elif code("401") or "unauthorized" in low or "invalid_client" in low or "oauth" in low:
        hint = "PAT rejected: wrong or expired token, or the role is not granted to the PAT's user."
    elif code("409") or "already exists" in low:
        hint = ("Name already taken in Snowflake. Horizon only lists Iceberg tables, so a native table "
                "or view with this name is invisible here: write into a schema without native objects.")
    elif denied and writing:
        hint = ("Write denied: the writer role needs CREATE ICEBERG TABLE on the schema and USAGE on its "
                "external volume, and the schema needs EXTERNAL_VOLUME and CATALOG = 'SNOWFLAKE' defaults.")
    elif "namespaces" in low and denied:
        hint = ("Schema not visible to the role (or the role tried to create one): grant USAGE on the "
                "schema, or create it in Snowflake first.")
    elif code("404") or "not found" in low or "does not exist" in low:
        hint = "Not found: database/schema/table names must exist, match case (usually upper) and be visible to the role."
    elif denied:
        hint = ("S3 read denied: the table owner role must keep USAGE on the external volume, "
                "and vended credentials must be enabled.")
    else:
        return msg
    first = msg.splitlines()[0] if msg else ""
    return f"{hint}\n  ({first})"


class HorizonCatalog:
    """One DuckDB connection with Horizon secrets and attached databases."""

    def __init__(self, snowflake: SnowflakeConfig, con: duckdb.DuckDBPyConnection | None = None):
        self.snowflake = snowflake
        self.con = con or duckdb.connect()
        self._extensions_loaded = False
        self.attached: dict[str, str] = {}      # alias -> Snowflake database

    def _load_extensions(self) -> None:
        if not self._extensions_loaded:
            try:
                self.con.execute("INSTALL iceberg; LOAD iceberg; INSTALL httpfs; LOAD httpfs;")
            except duckdb.Error as exc:
                raise CatalogError(f"Could not load the DuckDB iceberg/httpfs extensions: {exc}") from None
            self._extensions_loaded = True

    def add_secret(self, name: str, pat: str, role: str) -> None:
        self._load_extensions()
        self.con.execute(f"""
            CREATE OR REPLACE SECRET {sql_ident(name)} (
              TYPE iceberg,
              CLIENT_ID '',
              CLIENT_SECRET {sql_str(pat)},
              OAUTH2_SERVER_URI {sql_str(self.snowflake.oauth_token_uri)},
              OAUTH2_SCOPE {sql_str('session:role:' + role)}
            )""")

    def attach(self, database: str, *, secret: str, alias: str | None = None, write: bool = False) -> str:
        alias = alias or database
        if alias.upper() in BUILTIN_CATALOGS:
            raise CatalogError(f"Can't attach {database} as {alias}: DuckDB already uses that name")
        options = [
            "TYPE iceberg",
            f"SECRET {sql_ident(secret)}",
            f"ENDPOINT {sql_str(self.snowflake.catalog_uri)}",
            "ACCESS_DELEGATION_MODE 'vended_credentials'",
        ]
        if write:
            options += [f"{k} {'true' if v else 'false'}" for k, v in HORIZON_WRITE_OPTIONS.items()]
        else:
            options.append("READ_ONLY")
        try:
            self.con.execute(f"ATTACH {sql_str(database)} AS {sql_ident(alias)} ({', '.join(options)})")
        except duckdb.Error as exc:
            raise CatalogError(f"Attach {database} failed: {explain(exc)}") from None
        self.attached[alias] = database
        return alias

    def attach_local(self, path: str | Path, alias: str) -> str:
        """Test hook: a local DuckDB file stands in for a Horizon database (same SQL paths)."""
        self.con.execute(f"ATTACH {sql_str(str(path))} AS {sql_ident(alias)} (READ_ONLY)")
        self.attached[alias] = alias
        return alias

    def list_schemas(self, alias: str) -> list[str]:
        rows = self.con.execute(
            "SELECT schema_name FROM duckdb_schemas() WHERE database_name = ? AND NOT internal "
            "ORDER BY schema_name", [alias]).fetchall()
        return [r[0] for r in rows if r[0] not in ("main", "information_schema", "pg_catalog")]

    def list_tables(self, alias: str) -> list[TableRef]:
        try:
            rows = self.con.execute(
                "SELECT schema_name, table_name FROM duckdb_tables() WHERE database_name = ? "
                "ORDER BY schema_name, table_name", [alias]).fetchall()
        except duckdb.Error as exc:
            raise CatalogError(f"Listing tables in {alias} failed: {explain(exc)}") from None
        return [TableRef(alias, schema, name) for schema, name in rows]

    def probe(self, table: TableRef) -> str | None:
        """Read one row, so the vended S3 credentials are exercised. None if OK, else the explained error."""
        try:
            self.con.execute(f"SELECT * FROM {table.sql()} LIMIT 1").fetchall()
        except duckdb.Error as exc:
            return explain(exc)
        return None

    def close(self) -> None:
        self.con.close()
