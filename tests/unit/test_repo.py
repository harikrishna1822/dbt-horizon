from pathlib import Path

import pytest

from dbt_horizon.errors import RepoError
from dbt_horizon.repo import find_dbt_projects, redact, repo_slug, select_project, sync_repo
from tests.conftest import git


def test_redact_and_slug():
    assert redact("https://user:secret@github.com/org/repo.git") == "https://github.com/org/repo.git"
    assert redact("https://github.com/org/repo.git") == "https://github.com/org/repo.git"
    slug = repo_slug("https://github.com/org/repo.git")
    assert slug.startswith("org_repo-") and len(slug) == len("org_repo-") + 8
    assert repo_slug("https://git.anavsan.com:8443/group/sub/repo").startswith("sub_repo-")
    # same repo with or without credentials -> same cache; different host -> different cache
    assert repo_slug("https://user:pw@github.com/org/repo.git") == slug
    assert repo_slug("https://gitlab.com/org/repo.git") != slug
    assert len(repo_slug("file:///C:/" + "very-long-folder/" * 20 + "repo")) <= 49


def test_clone_then_fetch_new_commit(tmp_path, origin_repo):
    url = origin_repo.as_uri()
    first = sync_repo(url, "main", tmp_path / "cache")
    assert first.commit == git("rev-parse", "HEAD", cwd=origin_repo)
    assert (first.path / "retail_analytics_dbt" / "dbt_project.yml").is_file()

    (origin_repo / "retail_analytics_dbt" / "models" / "dim_product.sql").write_text("select 1\n")
    git("add", ".", cwd=origin_repo)
    git("commit", "-q", "-m", "second", cwd=origin_repo)
    second = sync_repo(url, "main", tmp_path / "cache")
    assert second.commit == git("rev-parse", "HEAD", cwd=origin_repo) != first.commit
    assert (second.path / "retail_analytics_dbt" / "models" / "dim_product.sql").is_file()


def test_pin_commit_and_tag(tmp_path, origin_repo):
    url = origin_repo.as_uri()
    first_sha = git("rev-parse", "HEAD", cwd=origin_repo)
    git("tag", "v1", cwd=origin_repo)
    (origin_repo / "later.txt").write_text("x\n")
    git("add", ".", cwd=origin_repo)
    git("commit", "-q", "-m", "later", cwd=origin_repo)
    assert sync_repo(url, first_sha, tmp_path / "c").commit == first_sha
    assert sync_repo(url, "v1", tmp_path / "c").commit == first_sha
    with pytest.raises(RepoError, match="not a branch, tag or commit"):
        sync_repo(url, "no-such-branch", tmp_path / "c")


def test_local_changes_in_cache_are_discarded(tmp_path, origin_repo):
    checkout = sync_repo(origin_repo.as_uri(), "main", tmp_path / "c")
    (checkout.path / "README.md").write_text("edited\n")
    (checkout.path / "stray.txt").write_text("x\n")
    again = sync_repo(origin_repo.as_uri(), "main", tmp_path / "c")
    assert (again.path / "README.md").read_text() == "# test repo\n"
    assert not (again.path / "stray.txt").exists()


def test_token_never_stored(tmp_path, origin_repo):
    checkout = sync_repo(origin_repo.as_uri(), "main", tmp_path / "c", token="tok-123")
    assert "tok-123" not in (checkout.path / ".git" / "config").read_text()


def test_credentials_in_url_rejected(tmp_path):
    with pytest.raises(RepoError, match="token_env"):
        sync_repo("https://user:pw@github.com/org/repo.git", "main", tmp_path)


def test_find_and_select_projects(tmp_path):
    for rel in ["a", "b/c", "a/dbt_packages/pkg", "a/target/x"]:
        (tmp_path / rel).mkdir(parents=True)
        (tmp_path / rel / "dbt_project.yml").write_text("name: x\n")
    assert find_dbt_projects(tmp_path) == ["a", "b/c"]
    assert select_project(tmp_path, "b\\c") == "b/c"
    with pytest.raises(RepoError, match="Several dbt projects"):
        select_project(tmp_path, None)
    with pytest.raises(RepoError, match="has no dbt_project.yml"):
        select_project(tmp_path, "zzz")


def test_root_project(tmp_path: Path):
    (tmp_path / "dbt_project.yml").write_text("name: x\n")
    assert select_project(tmp_path, None) == "."
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RepoError, match="No dbt_project.yml"):
        select_project(empty, None)
