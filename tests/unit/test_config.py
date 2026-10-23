from pathlib import Path

import pytest
import yaml

from dbt_horizon.config import AppConfig, build_env, load_config, read_env_file, resolve_secret
from dbt_horizon.errors import ConfigError

EXAMPLE = Path(__file__).parents[2] / "examples" / "snowflake-dbt.yml"


def write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_example_config_loads():
    cfg = load_config(EXAMPLE)
    assert cfg.snowflake.catalog_uri == "https://fblpisn-umc02429.snowflakecomputing.com/polaris/api/catalog"
    assert cfg.snowflake.oauth_token_uri.endswith("/polaris/api/catalog/v1/oauth/tokens")
    assert cfg.source_mapping[0].match.schema_name == "CORE"
    assert cfg.source_mapping[0].to.schema_name == "CORE_ICEBERG_S3"
    assert cfg.target_naming.schema_template == "{schema}_ICEBERG"
    assert cfg.catalog_databases() == ["RETAIL_ANALYTICS"]
    # relative workspace_root resolves against the config file, not the cwd
    assert cfg.workspace_root == (EXAMPLE.parent / "../.dbt_horizon").resolve()


def test_defaults(tmp_path, minimal_config):
    cfg = load_config(write(tmp_path, minimal_config))
    assert cfg.mode == "read"
    assert cfg.repo.ref == "main"
    assert cfg.unsupported_policy == "exclude"
    assert cfg.target_naming.identifier_case == "upper"
    assert cfg.workspace_root == tmp_path / ".dbt_horizon"


def test_write_mode_needs_writer(tmp_path, minimal_config):
    with pytest.raises(ConfigError, match="needs snowflake.writer"):
        load_config(write(tmp_path, {**minimal_config, "mode": "write"}))


@pytest.mark.parametrize("change, message", [
    ({"snowflake": {"account": "bad account!"}}, "should look like"),
    ({"snowflake": {"databases": ["RETAIL-ANALYTICS"]}}, "not a plain Snowflake identifier"),
    ({"snowflake": {"reader": {"role": "R", "pat_env": "not a var"}}}, "not an environment variable name"),
    ({"target_naming": {"schema": "{schema}"}}, "onto itself"),
    ({"target_naming": {"schema": "{table}_X"}}, "may only use"),
    ({"source_mapping": [{"match": {}, "to": {"schema": "X"}}]}, "needs database and/or schema"),
    ({"unknown_key": 1}, "Extra inputs are not permitted"),
])
def test_invalid_settings(tmp_path, minimal_config, change, message):
    data = {**minimal_config}
    for key, value in change.items():
        data[key] = {**data[key], **value} if isinstance(value, dict) and key in data else value
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, data))


def test_missing_and_malformed_files(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yml")
    bad = tmp_path / "bad.yml"
    bad.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(bad)


def test_catalog_databases_from_mapping(minimal_config):
    cfg = AppConfig.model_validate({**minimal_config, "source_mapping": [
        {"match": {"database": "RAW", "schema": "CORE"}, "to": {"database": "LAKE", "schema": "CORE"}},
        {"match": {"database": "RAW", "schema": "OPS"}, "to": {"schema": "OPS_ICEBERG"}},
    ]})
    assert cfg.catalog_databases() == ["RAW", "LAKE"]


def test_env_file_and_precedence(tmp_path, monkeypatch):
    env_file = tmp_path / "x.env"
    env_file.write_text("# comment\nexport A='1'\nB=\"two\"\nC=3\nnot a pair\n")
    assert read_env_file(env_file) == {"A": "1", "B": "two", "C": "3"}
    monkeypatch.setenv("C", "from-shell")
    env = build_env(env_file)
    assert env["A"] == "1" and env["C"] == "from-shell"


def test_resolve_secret():
    assert resolve_secret("PAT", {"PAT": " abc "}, "reader PAT") == "abc"
    with pytest.raises(ConfigError, match="PAT is not set \\(reader PAT\\)"):
        resolve_secret("PAT", {"PAT": ""}, "reader PAT")
