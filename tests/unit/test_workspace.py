import json
import os

from dbt_horizon.repo import sync_repo
from dbt_horizon.workspace import create_workspace


def test_workspace_is_a_clean_copy(tmp_path, origin_repo):
    checkout = sync_repo(origin_repo.as_uri(), "main", tmp_path / "c")
    # build/debris that must never reach the workspace
    for rel in ["retail_analytics_dbt/target/run.sql", "retail_analytics_dbt/logs/dbt.log",
                "retail_analytics_dbt/local.duckdb", "retail_analytics_dbt/.env"]:
        p = checkout.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")

    ws = create_workspace(checkout, "retail_analytics_dbt", tmp_path / "c")
    assert ws.root.name == checkout.commit[:12]
    assert ws.root.parent.parent == tmp_path / "c" / "workspaces"
    assert (ws.project_path / "dbt_project.yml").is_file()
    assert (ws.root / "README.md").is_file()
    assert not (ws.root / ".git").exists()
    for rel in ["target", "logs", "local.duckdb", ".env"]:
        assert not (ws.project_path / rel).exists(), rel

    meta = json.loads((ws.metadata_path / "workspace.json").read_text())
    assert meta == {"repo_url": checkout.url, "ref": "main", "commit": checkout.commit,
                    "project_dir": "retail_analytics_dbt"}


def test_workspace_rebuild_removes_long_paths(tmp_path, origin_repo):
    """dbt leaves target/compiled/... files whose full path is over 260 characters."""
    checkout = sync_repo(origin_repo.as_uri(), "main", tmp_path / "c")
    ws = create_workspace(checkout, "retail_analytics_dbt", tmp_path / "c")
    deep = ws.project_path / "target" / ("d" * 120) / ("e" * 120)
    long_file = deep / ("not_null_" + "x" * 60 + ".sql")
    prefix = "\\\\?\\" if os.name == "nt" else ""
    os.makedirs(prefix + str(deep.resolve()))
    with open(prefix + str(long_file.resolve()), "w") as f:
        f.write("select 1")
    assert len(str(long_file.resolve())) > 260
    ws2 = create_workspace(checkout, "retail_analytics_dbt", tmp_path / "c")
    assert not (ws2.project_path / "target").exists()


def test_workspace_rebuilt_each_time(tmp_path, origin_repo):
    checkout = sync_repo(origin_repo.as_uri(), "main", tmp_path / "c")
    ws = create_workspace(checkout, "retail_analytics_dbt", tmp_path / "c")
    (ws.project_path / "leftover.sql").write_text("x")
    ws2 = create_workspace(checkout, "retail_analytics_dbt", tmp_path / "c")
    assert ws2.root == ws.root
    assert not (ws2.project_path / "leftover.sql").exists()
