"""Generated profiles.yml. PATs are referenced with env_var(); values never reach the file."""
from __future__ import annotations

from pathlib import Path

import yaml

from ..config import SnowflakeConfig

READ_TARGET = "horizon_read"
READER_PAT_ENV = "DBT_HORIZON_READER_PAT"
READER_SECRET = "horizon_reader"


def _secret(sf: SnowflakeConfig, name: str, pat_env: str, role: str) -> dict:
    return {
        "type": "iceberg",
        "name": name,
        "client_id": "",
        "client_secret": "{{ env_var('" + pat_env + "') }}",
        "oauth2_server_uri": sf.oauth_token_uri,
        "oauth2_scope": f"session:role:{role}",
    }


def _reader_attach(sf: SnowflakeConfig, database: str) -> dict:
    return {
        "path": database,
        "alias": database,      # same name: fully qualified Snowflake SQL resolves unchanged
        "type": "iceberg",
        "secret": READER_SECRET,
        "read_only": True,
        "options": {"endpoint": sf.catalog_uri, "access_delegation_mode": "vended_credentials"},
    }


def read_profile(profile_name: str, sf: SnowflakeConfig, databases: list[str],
                 output_path: Path, schema: str, threads: int = 4,
                 local_catalog_dir: Path | None = None) -> dict:
    """Models build into `output_path` (its file stem is the dbt database); every Snowflake
    database a source reads is attached read-only under its own name.

    `local_catalog_dir` (tests): attach <dir>/<DATABASE>.duckdb files instead of Horizon,
    same names and SQL, no extensions, secrets or network.
    """
    output = {"type": "duckdb", "path": str(output_path), "schema": schema, "threads": threads}
    if local_catalog_dir is None:
        output["extensions"] = ["httpfs", "iceberg"]
        output["secrets"] = [_secret(sf, READER_SECRET, READER_PAT_ENV, sf.reader.role)]
        output["attach"] = [_reader_attach(sf, db) for db in databases]
    else:
        output["attach"] = [{"path": str(Path(local_catalog_dir) / f"{db}.duckdb"), "alias": db, "read_only": True}
                            for db in databases]
    return {profile_name: {"target": READ_TARGET, "outputs": {READ_TARGET: output}}}


def write_profiles(profiles_dir: Path, profile: dict) -> Path:
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = profiles_dir / "profiles.yml"
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    return path
