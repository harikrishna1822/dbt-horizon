# dbt on DuckDB over Snowflake Horizon: application plan

Goal: an application that takes **any dbt repo** (GitHub or GitLab) built for Snowflake and
**any Snowflake account**, and runs that same dbt project on DuckDB against the account's
Snowflake-managed Iceberg tables through the Horizon Iceberg REST Catalog:

- **read mode**: sources read from Iceberg, models built into a local DuckDB file
- **write mode**: models written back as Snowflake-managed Iceberg tables

Every step is deterministic code (parsing, rules, templates). No AI in the pipeline.
The source repo is never modified: the app works on a generated copy (overlay), so the
Snowflake-side dbt run on `main` keeps working unchanged.

---

## 1. Principles

1. **Never edit the customer repo.** Clone, then generate a workspace copy with an overlay.
   Optional later: export the overlay as a branch/PR, still never touching `main`.
2. **Use dbt's own parser.** Inventory comes from `manifest.json` (`dbt parse`), not regexes
   over files. Sources, models, materializations, hooks, packages and macros are all there.
3. **Rules, not judgement.** Every incompatibility is a coded rule with one of four outcomes:
   `ok`, `auto_fixed`, `needs_mapping` (config input required), `unsupported` (reported, run blocked
   or model excluded).
4. **Fail before running.** Preflight checks (auth, catalog visibility, name clashes, types)
   run first and produce a report.
5. **Secrets only in env or a secret store.** Never in generated files, never in git.

---

## 2. What we did by hand in retail_analytics_dbt, and the rule that replaces it

| # | Manual fix (this POC) | Automated by |
|---|---|---|
| 1 | Wrote a DuckDB profile: iceberg secret, attach, `vended_credentials` | Profile generator (templates) |
| 2 | Attach options with boolean `false` silently dropped by dbt-duckdb | Profile generator always emits them as strings |
| 3 | Sources `CORE` -> `CORE_ICEBERG_S3` | Source mapper (rules + Horizon catalog lookup) |
| 4 | Staging views -> ephemeral (no views in Iceberg) | Materialization mapper |
| 5 | Stock `table` materialization renames, Iceberg REST has no rename | Shim materialization `iceberg_table` (drop + CTAS) |
| 6 | `SUM(int)` gives `HUGEINT`, Iceberg has no int128 | Shim materialization auto-casts unsupported types |
| 7 | Upper-case table names for Snowflake | Shim `generate_alias_name` (write mode) |
| 8 | 409 "already exists": target schema held native tables | Preflight name-clash check + target schema naming rule |
| 9 | Writer role, schema, grants, PAT | Setup SQL generator |
| 10 | PowerShell wrapper leaked env vars between runs | App passes env to dbt per run (no shell scripts) |
| 11 | Test-syntax deprecation edits | Not touched: repo files stay as-is |

Acceptance test for the app: `retail_analytics_dbt` from GitHub runs read mode and write mode,
31/31 passing, **with zero manual edits**.

---

## 3. Architecture

```
          config.yml (account, auth refs, repo, mode, mapping rules)
                         |
  [1 repo] clone/pull -> [2 inspect] dbt parse -> manifest.json
                         |
  [3 catalog] Horizon: list namespaces/tables, check access
                         |
  [4 analyze] rule engine -> compat report (ok / auto_fixed / needs_mapping / unsupported)
                         |
  [5 transform] workspace copy + overlay (profile, shim package, patched yml)
                         |
  [6 preflight] auth, visibility, name clashes, writer grants
                         |
  [7 run] dbt build (dbt subprocess, env per call) -> run_results.json
                         |
  [8 report] per-model status, timings, row counts; optional verify vs Snowflake
```

### Components (Python package `dbt_horizon`)

| Module | Responsibility |
|---|---|
| `config.py` | Typed config (pydantic): account, database list, auth refs, mode, mapping rules, naming rules |
| `repo.py` | Clone/fetch any git URL (GitHub, GitLab, self-hosted), pin branch/commit, token from env/credential helper |
| `workspace.py` | Create `.dbt_horizon/<repo>/<commit>/` copy, apply overlay, keep it reproducible |
| `inventory.py` + `dbt_cli.py` | Parse profile -> `dbt deps` + `dbt parse` in a subprocess -> load `manifest.json` into an inventory |
| `catalog.py` | DuckDB + iceberg extension: secret, attach, list namespaces/tables, probe read (`LIMIT 0`) |
| `snowflake_meta.py` | Metadata-only Snowflake calls with the PAT (`SHOW SCHEMAS/OBJECTS`, `SHOW GRANTS`), no warehouse |
| `rules/` | One file per rule family (sources, materializations, configs, hooks, packages, sql, types) |
| `transform/profile.py` | Render `profiles.yml` from templates (read target, write target) |
| `transform/project.py` | Round-trip edit `dbt_project.yml` (ruamel.yaml, keeps comments) |
| `transform/sources.py` | Rewrite source database/schema, or pass `--vars` when sources are var-driven |
| `transform/sql.py` | Snowflake -> DuckDB SQL: Jinja-masked sqlglot transpile, only where a rule flags it |
| `shim/` | dbt package copied into the workspace: materializations + macros + DuckDB compat macros |
| `setup_sql.py` | Render Snowflake setup SQL (writer role, target schemas, grants, service user, PAT) |
| `run.py` | Run dbt with generated profile and env, collect `run_results.json` |
| `report.py` | Compat report + run report (JSON + Markdown/HTML) |
| `cli.py` | `connect`, `inspect`, `analyze`, `setup-sql`, `run`, `report` |

Stack: Python 3.12, `dbt-core` + `dbt-duckdb` (pinned), `duckdb` 1.5.x (pinned, >= 1.5.4 for Horizon writes),
`sqlglot`, `ruamel.yaml`, `pydantic`, `typer`, `snowflake-connector-python` (metadata only), `pytest`.

---

## 4. Config (input per account + repo)

```yaml
repo:
  url: https://github.com/harikrishna1822/snowflake-dbt.git
  ref: main
  project_dir: retail_analytics_dbt        # auto-detected if one dbt_project.yml
snowflake:
  account: fblpisn-umc02429                # <org>-<account>
  reader: { role: DUCKDB_READER,     pat_env: SNOWFLAKE_PAT }
  writer: { role: DBT_DUCKDB_WRITER, pat_env: SNOWFLAKE_WRITE_PAT }   # write mode only
mode: read                                 # read | write
# Write mode attaches each database twice: <DB> (reader PAT, READ_ONLY) for sources and
# <DB>__OUT (writer PAT, Horizon write options) for models.
source_mapping:                            # first match wins; default = same name
  - match: { database: RETAIL_ANALYTICS, schema: CORE }
    to:    { schema: CORE_ICEBERG_S3 }
target_naming:                             # write mode
  schema: "{schema}_ICEBERG"               # never write into a schema that holds native tables
  identifier_case: upper
unsupported_policy: exclude                # exclude model (and its children) | fail
```

---

## 5. Pipeline in detail

### 5.1 repo
- `git clone --filter=blob:none` into a cache, `git fetch` + checkout pinned commit.
- Auth: HTTPS token from env (`GITHUB_TOKEN` / `GITLAB_TOKEN`) or the OS credential helper. No SSH required.
- Detect dbt project dir(s): every `dbt_project.yml`; config picks one if several.

### 5.2 inspect
- Write a throwaway DuckDB profile (`:memory:`) so `dbt parse` works without Snowflake creds.
- `dbt deps` then `dbt parse` in a dbt subprocess; load `target/manifest.json`.
- Inventory: sources (database, schema, identifier, whether set from `var()`), models
  (materialization, config: `cluster_by`, `transient`, `copy_grants`, `snowflake_warehouse`, ...),
  hooks, snapshots, seeds, Python models, packages, root-project macro overrides
  (`generate_schema_name`, `generate_alias_name`, `generate_database_name`), `target.type` branches.

### 5.3 catalog
- For each Snowflake database referenced by sources: create iceberg secret (reader PAT),
  `ATTACH '<DB>' AS <DB> (TYPE iceberg, ENDPOINT .../polaris/api/catalog, ACCESS_DELEGATION_MODE 'vended_credentials', READ_ONLY)`.
  Attaching under the database's own name lets fully qualified `DB.SCHEMA.TABLE` SQL resolve unchanged.
- List namespaces and tables; probe each mapped source with `SELECT * ... LIMIT 0`.

### 5.4 analyze (rule engine)

| Rule family | Check | Outcome |
|---|---|---|
| sources | mapped table exists in Horizon | `ok` / `needs_mapping` (native table: not served to external engines) |
| sources | masking / row access policy on table | `unsupported` (Horizon won't serve it) |
| materialization | `view` | read: keep (local file) ; write: -> `ephemeral` or `table` (config) |
| materialization | `table` | read: stock ; write: `iceberg_table` |
| materialization | `incremental` (append, delete+insert, merge) | read: stock ; write: `iceberg_incremental` (MERGE proven in POC) |
| materialization | `dynamic_table`, `materialized_view` | -> `table` (`auto_fixed`, reported) |
| materialization | `snapshot` | read: stock ; write: phase 2 |
| configs | `cluster_by`, `transient`, `copy_grants`, `query_tag`, `snowflake_warehouse`, `secure` | stripped (`auto_fixed`) |
| hooks | statements with `GRANT`, `ALTER WAREHOUSE`, `USE ...`, `CALL` | disabled (`auto_fixed`, listed) |
| python models | Snowpark | `unsupported` |
| packages | dispatch has no duckdb impl (e.g. snowflake-only packages) | `unsupported` per macro use |
| macros | root overrides `generate_schema_name` etc. | wrapped by shim (see 5.5) |
| sql | parse compiled SQL with sqlglot (dialect snowflake) | `ok` / `auto_fixed` (transpile) / `unsupported` (parse fail) |
| types | output columns after `DESCRIBE` | HUGEINT/UHUGEINT/unsigned/INTERVAL/ENUM cast or `unsupported` |
| naming | write target schema/table already exists as native object (`SHOW OBJECTS`) | `fail` preflight (the 409 case) |

### 5.5 transform (overlay in the workspace copy)

Files the app **generates** in the workspace (never in the repo):

| File | Content |
|---|---|
| `profiles.yml` | `horizon_read` / `horizon_write` targets: extensions, iceberg secrets with `env_var()` PATs, one attach per database, write options as strings |
| `dbt_packages/dbt_horizon_shim/` | shim package (below) |
| `.dbt_horizon/overlay.json` | everything changed, for audit and the report |

Files the app **patches** in the workspace copy:

| File | Patch |
|---|---|
| `dbt_project.yml` | add `dispatch` search order (root, shim, dbt); per-target `+materialized` map; `on-run-start: {{ dbt_horizon_shim.install_compat_macros() }}`; strip Snowflake-only configs |
| `packages.yml` | add local `dbt_horizon_shim` |
| `models/**/sources*.yml` | database/schema per `source_mapping` (skipped when sources are var-driven: passed as `--vars`) |
| root `macros/generate_schema_name.sql` (if present) | rename macro to `project_generate_schema_name`; shim's version calls it, then applies `target_naming` |
| `models/**/*.sql` | only for `sql` rule hits: Jinja-masked sqlglot transpile (snowflake -> duckdb) |

`dbt_horizon_shim` package:
- `materializations/iceberg_table.sql`: drop + CTAS, autocommit per statement, types auto-cast from a `DESCRIBE` of the query.
- `materializations/iceberg_incremental.sql`: MERGE / delete+insert / append into Iceberg.
- `macros/naming.sql`: `generate_schema_name`, `generate_alias_name` (upper case in write mode).
- `macros/compat/*.sql`: DuckDB `CREATE MACRO` library for Snowflake functions
  (`iff`, `nvl`, `nvl2`, `div0`, `zeroifnull`, `to_varchar`, `dateadd`, `datediff`, `array_size`, ...),
  installed once per run by `on-run-start`. Covers functions without touching model SQL.

### 5.6 preflight
- Reader PAT: token exchange OK, every source readable.
- Write mode: writer PAT OK, target schemas visible, `CREATE ICEBERG TABLE` granted, external volume usage,
  no native objects with the same names (`SHOW OBJECTS IN SCHEMA`), schema has `EXTERNAL_VOLUME` + `CATALOG='SNOWFLAKE'`.
- Missing pieces -> `setup-sql` renders the exact Snowflake script (roles, schemas, grants, service user, PAT).

### 5.7 run + report
- dbt subprocess `["build", "--target", ..., "--profiles-dir", ws, "--project-dir", ws, "--vars", ...]`
  with the env passed explicitly to the subprocess (no shell, no leakage).
- Report: compat outcomes, per-model status/time, rows written, excluded models and why.
- Optional `verify`: compare row counts / sums with the Snowflake-built models (needs a warehouse; off by default).

---

## 6. What changes in the customer's repo

Nothing by default. The overlay lives in the app's workspace.

Optional `export` (later phase): push the overlay to a branch such as `dbt-horizon/<date>` with
`profiles.yml` targets, the shim package and patched yml files, and open a PR. Requirement before
that: the patched project must still `dbt parse` with the Snowflake target on the dbt version
Snowflake runs, so the Snowflake side keeps working if the branch is merged.

---

## 7. Build phases

| Phase | Scope | Done when |
|---|---|---|
| **1. Foundation** | `config`, `repo`, `workspace`, `catalog`, CLI `connect` | Clones any repo, attaches Horizon for any account, lists Iceberg tables |
| **2. Inspect + analyze** | `inspect`, rule engine, compat report | Report for `snowflake-dbt` matches the table in section 2 |
| **3. Read mode** | profile generator, source mapper, materialization mapper, compat macros, `run` | `snowflake-dbt` read mode 31/31, zero manual edits |
| **4. Write mode** | shim materializations, type casts, naming, preflight, `setup-sql` | Write mode 31/31; native-name clash caught before running |
| **5. SQL layer** | Jinja-masked sqlglot transpile, EXPLAIN validation, more compat macros | Fixture repos with Snowflake-only SQL pass or are reported precisely |
| **6. Packaging** | pip package, CI templates (GitHub Actions, GitLab CI), docs | `pip install` + one config file runs end to end |
| **7. Export (optional)** | overlay as branch + PR | PR parses on both targets |

Testing throughout:
- **Offline**: fixture dbt projects + a local DuckDB file attached under the Snowflake database name
  instead of Horizon (same SQL paths, no network).
- **Online**: `snowflake-dbt` against the dev account (read and write), run in CI on demand.
- Unit tests per rule: input manifest snippet -> expected outcome and patch.

---

## 8. Decisions

| Topic | Decision |
|---|---|
| App form | CLI package first (`dbt-horizon`); a UI or service wraps the same package later |
| Write-mode auth | Reader PAT for sources (read-only attach) + writer PAT for target schemas (least privilege) |
| Unsupported models | Excluded together with their children; listed with the reason in the report |
| Code home | New GitHub repo `harikrishna1822/dbt-horizon`; `snowflake-dbt` is the test fixture |

| dbt invocation | Subprocess with an explicit env per call, not `dbtRunner` in-process: no state or open log files carried between runs (Windows locks them), PATs scoped to the call |
| Name resolution | Parse profile resolves names like the Snowflake target (database attached under its name, schema from the repo's `profiles.yml` or `snowflake.target`); unquoted source names are upper-cased as Snowflake does |

Still open:
1. Default for `view` in write mode: `ephemeral` (POC behaviour) or `table`?
2. Where the app runs: developer laptops only, or also a server/CI runner (affects network policy on the PATs)?
