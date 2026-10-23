"""dbt-horizon command line."""
from __future__ import annotations

import sys
from itertools import groupby
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .catalog import HorizonCatalog
from .config import AppConfig, build_env, load_config, resolve_secret
from .errors import ConfigError, DbtHorizonError
from .repo import redact, select_project, sync_repo
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


if __name__ == "__main__":
    app()
