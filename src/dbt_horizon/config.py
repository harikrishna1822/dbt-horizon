"""Typed config for one repo + one Snowflake account (config.yml).

Secrets are never in the config: it names the env vars that hold them
(`pat_env`, `token_env`). Values come from the process env or an env file
passed to the CLI, and are handed to subprocesses explicitly.
"""
from __future__ import annotations

import os
import re
import string
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ConfigError

# <org>-<account> or a legacy locator such as xy12345.eu-west-1
_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# Unquoted Snowflake identifier; Horizon resolves names exactly, so keep them as written.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_ident(value: str | None, what: str) -> str | None:
    if value is not None and not _IDENT.match(value):
        raise ValueError(f"{what} {value!r} is not a plain Snowflake identifier")
    return value


def _check_env_name(value: str | None) -> str | None:
    if value is not None and not _ENV_NAME.match(value):
        raise ValueError(f"{value!r} is not an environment variable name")
    return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class RepoConfig(_Model):
    url: str
    ref: str = "main"
    # Folder of dbt_project.yml inside the repo; auto-detected when the repo has one project.
    project_dir: str | None = None
    # Env var with an HTTPS token (GitHub/GitLab PAT). Unset: git's credential helper is used.
    token_env: str | None = None

    @field_validator("token_env")
    @classmethod
    def _token_env(cls, v: str | None) -> str | None:
        return _check_env_name(v)


class Credential(_Model):
    role: str
    pat_env: str

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        return _check_ident(v, "role")

    @field_validator("pat_env")
    @classmethod
    def _pat_env(cls, v: str) -> str:
        return _check_env_name(v)


class SchemaRef(_Model):
    database: str | None = None
    schema_name: str | None = Field(default=None, alias="schema")

    @model_validator(mode="after")
    def _idents(self) -> SchemaRef:
        _check_ident(self.database, "database")
        _check_ident(self.schema_name, "schema")
        if self.database is None and self.schema_name is None:
            raise ValueError("needs database and/or schema")
        return self


class SnowflakeConfig(_Model):
    account: str
    # Databases to attach for `connect`; `analyze` adds the ones dbt sources reference.
    databases: list[str] = []
    reader: Credential
    writer: Credential | None = None
    # Database/schema of the Snowflake dbt target. Default: the repo's profiles.yml.
    target: SchemaRef | None = None

    @field_validator("account")
    @classmethod
    def _account(cls, v: str) -> str:
        if not _ACCOUNT.match(v):
            raise ValueError(f"account {v!r} should look like <org>-<account>")
        return v

    @field_validator("databases")
    @classmethod
    def _databases(cls, v: list[str]) -> list[str]:
        return [_check_ident(d, "database") for d in v]

    @property
    def catalog_uri(self) -> str:
        return f"https://{self.account}.snowflakecomputing.com/polaris/api/catalog"

    @property
    def oauth_token_uri(self) -> str:
        return f"{self.catalog_uri}/v1/oauth/tokens"


class SourceMappingRule(_Model):
    match: SchemaRef
    to: SchemaRef


class TargetNaming(_Model):
    # Schema the write mode builds into, from the model's own schema. Must differ from it:
    # the original schema holds the Snowflake-side dbt run's native tables (same names).
    schema_template: str = Field(default="{schema}_ICEBERG", alias="schema")
    identifier_case: Literal["upper", "preserve"] = "upper"

    @field_validator("schema_template")
    @classmethod
    def _template(cls, v: str) -> str:
        fields = {f for _, f, _, _ in string.Formatter().parse(v) if f is not None}
        if not fields <= {"schema", "database"}:
            raise ValueError(f"schema template {v!r} may only use {{schema}} and {{database}}")
        if v == "{schema}":
            raise ValueError("schema template must not map a schema onto itself")
        return v


class AppConfig(_Model):
    repo: RepoConfig
    snowflake: SnowflakeConfig
    mode: Literal["read", "write"] = "read"
    source_mapping: list[SourceMappingRule] = []
    target_naming: TargetNaming = TargetNaming()
    unsupported_policy: Literal["exclude", "fail"] = "exclude"
    # App state (repo cache, workspaces). Relative paths resolve against the config file.
    workspace_root: Path = Path(".dbt_horizon")

    @model_validator(mode="after")
    def _writer_for_write_mode(self) -> AppConfig:
        if self.mode == "write" and self.snowflake.writer is None:
            raise ValueError("mode 'write' needs snowflake.writer (role + pat_env)")
        return self

    def catalog_databases(self) -> list[str]:
        """Databases to attach: the configured list plus any named in source mapping rules."""
        found: list[str] = []
        names = list(self.snowflake.databases)
        for rule in self.source_mapping:
            names += [rule.match.database, rule.to.database]
        for name in names:
            if name and name not in found:
                found.append(name)
        return found


def load_config(path: str | os.PathLike) -> AppConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    try:
        config = AppConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "\n".join(
            f"  {'.'.join(str(p) for p in err['loc']) or '(top level)'}: {err['msg']}"
            for err in exc.errors())
        raise ConfigError(f"{path} has invalid settings:\n{problems}") from None
    if not config.workspace_root.is_absolute():
        config = config.model_copy(update={"workspace_root": (path.parent / config.workspace_root).resolve()})
    return config


def read_env_file(path: str | os.PathLike) -> dict[str, str]:
    """KEY=VALUE lines (optional `export `, quotes stripped). Does not touch os.environ."""
    values: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise ConfigError(f"Env file not found: {path}") from None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def build_env(env_file: str | os.PathLike | None = None) -> dict[str, str]:
    """Process env overlaid on the env file: variables set in the shell win."""
    env = read_env_file(env_file) if env_file else {}
    env.update(os.environ)
    return env


def resolve_secret(env_name: str, env: Mapping[str, str], purpose: str) -> str:
    value = env.get(env_name, "").strip()
    if not value:
        raise ConfigError(f"{env_name} is not set ({purpose}). Set it in the shell or in the --env-file.")
    return value
