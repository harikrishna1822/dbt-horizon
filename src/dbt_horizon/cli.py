"""dbt-horizon command line."""
from __future__ import annotations

import sys
from collections import Counter
from itertools import groupby
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .catalog import HorizonCatalog
from .config import AppConfig, build_env, load_config, resolve_secret
from .errors import ConfigError, DbtHorizonError
from .inventory import Inventory, parse_project
from .repo import redact, select_project, sync_repo
from .report import summary_lines, write_report
from .rules import analyze as analyze_project
from .rules.sources import map_location
from .workspace import Workspace, create_workspace

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Run a Snowflake dbt project on DuckDB over Snowflake Horizon Iceberg tables.")

ConfigOpt = typer.Option(..., "--config", "-c", exists=True, dir_okay=False, help="config.yml for one repo + account")
EnvFileOpt = typer.Option(None, "--env-file", exists=True, dir_okay=False,
                          help="KEY=VALUE file with the PATs/tokens (shell variables win)")

READER_SECRET, WRITER_SECRET = "horizon_reader", "horizon_writer"


def _say(msg: str = "") -> None:
    typer.echo(msg)


def _run(step):
    """Run a command body; expected failures print a message and exit non-zero (no traceback)."""
    try:
        return step()
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(2)
    except DbtHorizonError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


@app.callback()
def _main() -> None:
    # Windows consoles default to cp1252; table and path output may not encode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


@app.command()
def version() -> None:
    """Print the version."""
    _say(__version__)


def prepare_workspace(cfg: AppConfig, env: dict[str, str]) -> Workspace:
    token = resolve_secret(cfg.repo.token_env, env, "repo token") if cfg.repo.token_env else None
    _say(f"Repo       {redact(cfg.repo.url)} @ {cfg.repo.ref}")
    checkout = sync_repo(cfg.repo.url, cfg.repo.ref, cfg.workspace_root, token=token)
    _say(f"Commit     {checkout.commit}")
    project_dir = select_project(checkout.path, cfg.repo.project_dir)
    ws = create_workspace(checkout, project_dir, cfg.workspace_root)
    _say(f"Project    {project_dir}")
    _say(f"Workspace  {ws.project_path}")
    return ws


@app.command()
def connect(config: Path = ConfigOpt, env_file: Optional[Path] = EnvFileOpt,
            probe: bool = typer.Option(False, "--probe", help="read one row of every table (checks S3 access)"),
            no_repo: bool = typer.Option(False, "--no-repo", help="skip the repo, only check Horizon")) -> None:
    """Fetch the repo, attach the account's databases through Horizon and list their Iceberg tables."""
    def body() -> None:
        cfg = load_config(config)
        env = build_env(env_file)
        if not no_repo:
            prepare_workspace(cfg, env)
            _say()

        databases = cfg.catalog_databases()
        if not databases:
            raise ConfigError("No databases to attach: set snowflake.databases (or source_mapping)")
        sf = cfg.snowflake
        catalog = HorizonCatalog(sf)
        failures = 0
        try:
            catalog.add_secret(READER_SECRET, resolve_secret(sf.reader.pat_env, env, "reader PAT"), sf.reader.role)
            _say(f"Horizon    {sf.catalog_uri}  (reader role {sf.reader.role})")
            for database in databases:
                catalog.attach(database, secret=READER_SECRET)
                tables = catalog.list_tables(database)
                _say(f"\n{database}: {len(tables)} Iceberg table(s)")
                for schema, group in groupby(tables, key=lambda t: t.schema):
                    _say(f"  {schema}")
                    for table in group:
                        status = ""
                        if probe:
                            error = catalog.probe(table)
                            failures += error is not None
                            status = "  OK" if error is None else f"  FAILED: {error}"
                        _say(f"    {table.name}{status}")

            if sf.writer is not None:
                catalog.add_secret(WRITER_SECRET, resolve_secret(sf.writer.pat_env, env, "writer PAT"),
                                   sf.writer.role)
                _say(f"\nWriter role {sf.writer.role} sees schemas:")
                for database in databases:
                    alias = catalog.attach(database, secret=WRITER_SECRET, alias=f"{database}__OUT", write=True)
                    _say(f"  {database}: {', '.join(catalog.list_schemas(alias)) or '(none)'}")
        finally:
            catalog.close()
        if failures:
            typer.echo(f"\n{failures} table(s) could not be read.", err=True)
            raise typer.Exit(1)
        _say("\nConnection OK.")

    _run(body)


def _parse(cfg: AppConfig, ws: Workspace, env: dict[str, str]) -> Inventory:
    target = cfg.snowflake.target
    inv = parse_project(ws, env, target=(target.database, target.schema_name) if target else (None, None))
    _say(f"dbt        {inv.dbt_version}, project {inv.project_name}, "
         f"Snowflake target {inv.target_database}.{inv.target_schema}")
    return inv


def _horizon_tables(cfg: AppConfig, inv: Inventory, env: dict[str, str]) -> set[str]:
    """Upper-case DB.SCHEMA.TABLE of every Iceberg table the reader role sees, in the
    configured databases and every database a (mapped) source points to."""
    databases = cfg.catalog_databases()
    for src in inv.sources:
        mapped_db = map_location(src.database, src.schema, list(cfg.source_mapping))[0]
        if mapped_db and mapped_db not in databases:
            databases.append(mapped_db)
    sf = cfg.snowflake
    catalog = HorizonCatalog(sf)
    try:
        catalog.add_secret(READER_SECRET, resolve_secret(sf.reader.pat_env, env, "reader PAT"), sf.reader.role)
        tables = set()
        for database in databases:
            catalog.attach(database, secret=READER_SECRET)
            tables |= {str(t).upper() for t in catalog.list_tables(database)}
    finally:
        catalog.close()
    _say(f"Horizon    {len(tables)} Iceberg table(s) in {', '.join(databases)} (reader role {sf.reader.role})")
    return tables


@app.command("inspect")
def inspect_cmd(config: Path = ConfigOpt, env_file: Optional[Path] = EnvFileOpt) -> None:
    """Parse the dbt project and print its inventory (no Snowflake connection)."""
    def body() -> None:
        cfg = load_config(config)
        env = build_env(env_file)
        inv = _parse(cfg, prepare_workspace(cfg, env), env)
        _say()
        by_kind = Counter((n.resource_type, n.materialized or "") for n in inv.of_type("model", "seed", "snapshot"))
        for (kind, materialized), count in sorted(by_kind.items()):
            _say(f"  {count:4} {kind}{' (' + materialized + ')' if materialized else ''}")
        _say(f"  {len(inv.of_type('test')):4} test")
        _say(f"  {len(inv.sources):4} source")
        for src in inv.sources:
            via = f"  (via var: {', '.join(f'{k}={v}' for k, v in src.vars.items())})" if src.vars else ""
            _say(f"         {src}{via}")
        hooks = sum(len(v) for v in inv.project_hooks.values())
        _say(f"  project hooks: {hooks}, packages: {len(inv.packages)}, "
             f"naming overrides: {', '.join(inv.root_macro_overrides) or 'none'}")

    _run(body)


@app.command()
def analyze(config: Path = ConfigOpt, env_file: Optional[Path] = EnvFileOpt,
            mode: Optional[str] = typer.Option(None, "--mode", help="read | write (default: config mode)"),
            offline: bool = typer.Option(False, "--offline", help="don't check sources against Horizon")) -> None:
    """Parse the project, apply the compatibility rules and write the report."""
    def body() -> None:
        cfg = load_config(config)
        if mode not in (None, "read", "write"):
            raise ConfigError("--mode must be read or write")
        if mode == "write" and cfg.snowflake.writer is None:
            raise ConfigError("--mode write needs snowflake.writer in the config")
        env = build_env(env_file)
        ws = prepare_workspace(cfg, env)
        inv = _parse(cfg, ws, env)
        tables = None if offline else _horizon_tables(cfg, inv, env)
        analysis = analyze_project(inv, cfg, tables, mode=mode)
        json_path, md_path = write_report(ws, inv, analysis)
        _say()
        for line in summary_lines(analysis):
            _say(line)
        _say(f"\nReport     {md_path}\n           {json_path}")
        if not analysis.runnable:
            raise typer.Exit(1)

    _run(body)


if __name__ == "__main__":
    app()
