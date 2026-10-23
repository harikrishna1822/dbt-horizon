# dbt-horizon

Run a Snowflake dbt project on DuckDB against the account's Snowflake-managed Iceberg tables,
through the Snowflake Horizon Iceberg REST Catalog. Works for any dbt repo (GitHub, GitLab,
self-hosted) and any Snowflake account; the customer repo is never modified.

Design: [docs/design.md](docs/design.md). Status: **Phase 1 (foundation)**: config, repo sync,
workspace, Horizon connection, `connect` command.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Configure

One YAML file per repo + account; secrets are referenced by env var name only.
See [examples/snowflake-dbt.yml](examples/snowflake-dbt.yml).

```powershell
Copy-Item examples\snowflake-dbt.env.example examples\snowflake-dbt.env   # fill in the PATs (git-ignored)
```

| Setting | Meaning |
|---|---|
| `repo.url`, `repo.ref` | git URL and branch/tag/commit to run |
| `repo.project_dir` | folder of `dbt_project.yml` (auto-detected if the repo has one) |
| `repo.token_env` | env var with an HTTPS token, for private repos |
| `snowflake.account` | `<org>-<account>` |
| `snowflake.databases` | databases to attach |
| `snowflake.reader` / `writer` | role + env var of its PAT (writer: write mode only) |
| `source_mapping` | where the Iceberg copy of a source schema lives (e.g. `CORE` -> `CORE_ICEBERG_S3`) |
| `target_naming.schema` | write-mode target schema, e.g. `{schema}_ICEBERG` |

## Commands

```powershell
dbt-horizon connect -c examples\snowflake-dbt.yml --env-file examples\snowflake-dbt.env --probe
```

`connect` fetches the repo at the pinned ref into `.dbt_horizon/repos/`, builds a workspace copy
under `.dbt_horizon/workspaces/<repo>/<commit>/`, attaches each database through Horizon with
the reader PAT (read-only), lists the Iceberg tables (`--probe` reads one row of each), and, if a
writer is configured, lists the schemas the writer role can see.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest                      # unit + offline, no network
$env:DBT_HORIZON_ONLINE = "1"; $env:DBT_HORIZON_ENV_FILE = "examples\snowflake-dbt.env"
.\.venv\Scripts\python.exe -m pytest tests/online         # against the real account
```

Offline tests use local git repos and a local DuckDB file attached under the Snowflake
database name, so they exercise the same code paths without Snowflake.
