import subprocess
from pathlib import Path

import pytest


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    """A local git repo standing in for GitHub/GitLab: one dbt project in a subfolder."""
    repo = tmp_path / "origin"
    project = repo / "retail_analytics_dbt"
    (project / "models").mkdir(parents=True)
    (repo / "README.md").write_text("# test repo\n")
    (project / "dbt_project.yml").write_text("name: retail_analytics_dbt\nprofile: retail_analytics_dbt\n")
    (project / "models" / "dim_store.sql").write_text("select 1 as STORE_SK\n")
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "test@example.com", cwd=repo)
    git("config", "user.name", "test", cwd=repo)
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "initial", cwd=repo)
    return repo


@pytest.fixture
def minimal_config() -> dict:
    return {
        "repo": {"url": "https://github.com/org/repo.git"},
        "snowflake": {
            "account": "myorg-myaccount",
            "reader": {"role": "DUCKDB_READER", "pat_env": "SNOWFLAKE_PAT"},
        },
    }
