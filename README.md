# dbt-horizon

Run a Snowflake dbt project on DuckDB against the account's Snowflake-managed Iceberg tables,
through the Snowflake Horizon Iceberg REST Catalog. Works for any dbt repo (GitHub, GitLab,
self-hosted) and any Snowflake account; the customer repo is never modified.

Design: [docs/design.md](docs/design.md). Status:
- **Phase 1 (foundation)**: config, repo sync, workspace, Horizon connection, `connect`.
- **Phase 2 (inspect + analyze)**: dbt-parsed inventory, rule engine, compatibility report.
- **Phase 3 (read mode)**: overlay in the workspace, `dbt build` on DuckDB into a local file.

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
| `snowflake.target` | database/schema of the Snowflake dbt target (default: the repo's `profiles.yml`) |
| `target_naming.schema` | write-mode target schema, e.g. `{schema}_ICEBERG` |
| `unsupported_policy` | `exclude` (skip blocked models and everything downstream) or `fail` |

## Commands

```powershell
dbt-horizon connect -c examples\snowflake-dbt.yml --env-file examples\snowflake-dbt.env --probe
```

`connect` fetches the repo at the pinned ref into `.dbt_horizon/repos/`, builds a workspace copy
under `.dbt_horizon/workspaces/<repo>/<commit>/`, attaches each database through Horizon with
the reader PAT (read-only), lists the Iceberg tables (`--probe` reads one row of each), and, if a
writer is configured, lists the schemas the writer role can see.

```powershell
dbt-horizon inspect -c examples\snowflake-dbt.yml
dbt-horizon analyze -c examples\snowflake-dbt.yml --env-file examples\snowflake-dbt.env --mode write
```

`inspect` runs `dbt parse` in the workspace against a throwaway DuckDB profile that resolves
names like the Snowflake target (no Snowflake credentials), and prints the inventory: models by
materialization, tests, sources (and which take their location from `var()`), hooks, packages.

`analyze` applies the rules and writes `compat_report_<mode>.md` / `.json` in the workspace's
`.dbt_horizon/` folder. Every finding has one outcome:

| Outcome | Meaning |
|---|---|
| `auto_fixed` | the overlay changes it deterministically (e.g. view -> ephemeral, `--vars` remap) |
| `review` | runs, but a person should check it (e.g. FLATTEN until the transpile layer) |
| `needs_mapping` | config input required (e.g. source has no Iceberg copy in Horizon) |
| `unsupported` | can't run on DuckDB (e.g. Python/Snowpark models, CONNECT BY) |

Blocking outcomes (`needs_mapping`, `unsupported`) exclude the node and everything downstream
(policy `exclude`), or stop the run (policy `fail`, exit code 1). `--offline` skips the Horizon
check (sources become `review`).

```powershell
dbt-horizon run -c examples\snowflake-dbt.yml --env-file examples\snowflake-dbt.env --mode read
```

`run` analyzes, applies the overlay to the workspace copy and runs `dbt build` (in a subprocess,
the reader PAT passed only to it). Models build into
`.dbt_horizon/outputs/<repo>/<project>_local.duckdb`, schemas named as in Snowflake.

Read-mode overlay (all in the workspace, listed in `.dbt_horizon/overlay.json`):

| Change | Why |
|---|---|
| generated `profiles.yml` (target `horizon_read`) | local output file + every source database attached read-only through Horizon under its own name; PAT via `env_var('DBT_HORIZON_READER_PAT')` |
| `--vars` / sources yml rewrite | sources read their Iceberg copy (`source_mapping`) |
| `--exclude <node>+` | blocked nodes and everything downstream |
| `macros/dbt_horizon/compat.sql` + `on-run-start` hook | DuckDB macros for IFF, NVL, NVL2, DIV0, ZEROIFNULL, NULLIFZERO, TO_VARCHAR, ARRAY_SIZE |
| `macros/dbt_horizon/run_hooks.sql` | model hooks that are Snowflake admin statements (GRANT, ...) are skipped |
| `macros/dbt_horizon/naming.sql` | every model builds into the local database (`generate_database_name`) |
| `macros/dbt_horizon/materialization_<name>.sql` | `dynamic_table` / `materialized_view` build as tables |
| `dbt_project.yml` hooks | Snowflake admin statements removed from on-run-start/end |

Staging views keep their Snowflake materialization; they read the Horizon attach, so they work
during dbt runs (and in any session that attaches the same databases), tables work anywhere.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest                      # unit + offline, no network
$env:DBT_HORIZON_ONLINE = "1"; $env:DBT_HORIZON_ENV_FILE = "examples\snowflake-dbt.env"
.\.venv\Scripts\python.exe -m pytest tests/online         # against the real account
```

Offline tests use local git repos and a local DuckDB file attached under the Snowflake
database name, so they exercise the same code paths without Snowflake.
