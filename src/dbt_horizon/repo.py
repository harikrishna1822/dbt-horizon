"""Fetch the customer's dbt repo into the app's own cache clone.

Read-only towards the remote: the app never commits or pushes. The cache clone
belongs to the app, so it is checked out with --force at the pinned commit.
Works with any git URL (GitHub, GitLab, self-hosted, file://, local path).
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from .errors import RepoError

# Never searched for dbt_project.yml (installed packages, build output, envs).
_SKIP_DIRS = {".git", "dbt_packages", "target", "logs", ".venv", "venv", "node_modules", "__pycache__"}


@dataclass(frozen=True)
class Checkout:
    url: str          # redacted
    ref: str
    commit: str
    path: Path


def redact(url: str) -> str:
    """Drop user:password@ from a URL for display and metadata."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return url


def repo_slug(url: str) -> str:
    """Short, stable folder name for a repo URL, e.g. org_repo-1a2b3c4d.

    Last two path segments for readability plus a hash of the whole URL for
    uniqueness; kept short because Windows paths are limited to 260 characters.
    """
    clean = redact(url)
    path = re.sub(r"\.git$", "", urlsplit(clean).path.replace("\\", "/").strip("/"))
    name = "_".join(path.split("/")[-2:])
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_.")[:40] or "repo"
    return f"{name}-{hashlib.sha1(clean.encode()).hexdigest()[:8]}"


def _git(args: list[str], cwd: Path | None = None, token: str | None = None) -> str:
    env = dict(os.environ)
    if token:
        # Basic auth works for GitHub and GitLab tokens (the user name is ignored).
        # Passed via GIT_CONFIG_* so it is neither in argv nor in .git/config.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            "GIT_TERMINAL_PROMPT": "0",
        })
    try:
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise RepoError("git is not installed or not on PATH") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if token:
            detail = detail.replace(token, "***")
        raise RepoError(f"git {args[0]} failed: {detail}")
    return result.stdout


def sync_repo(url: str, ref: str, cache_root: Path, token: str | None = None) -> Checkout:
    """Clone (first time) or fetch, then check out `ref` (branch, tag or commit) detached."""
    if urlsplit(url).password:
        raise RepoError("Put the token in repo.token_env, not in the repo URL")
    path = Path(cache_root) / "repos" / repo_slug(url)
    if (path / ".git").is_dir():
        current = _git(["remote", "get-url", "origin"], cwd=path).strip()
        if current != url:
            _git(["remote", "set-url", "origin", url], cwd=path)
        _git(["fetch", "--prune", "--tags", "--force", "origin"], cwd=path, token=token)
    else:
        if path.exists():
            shutil.rmtree(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--no-checkout", "-c", "core.autocrlf=false", "-c", "core.longpaths=true",
              url, str(path)], token=token)
    commit = resolve_ref(path, ref)
    _git(["checkout", "--force", "--detach", commit], cwd=path, token=token)
    _git(["clean", "-ffdxq"], cwd=path)
    return Checkout(url=redact(url), ref=ref, commit=commit, path=path)


def resolve_ref(path: Path, ref: str) -> str:
    for candidate in (f"refs/remotes/origin/{ref}", f"refs/tags/{ref}", ref):
        try:
            return _git(["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], cwd=path).strip()
        except RepoError:
            continue
    raise RepoError(f"Ref {ref!r} is not a branch, tag or commit of the repo")


def find_dbt_projects(root: Path) -> list[str]:
    """Repo-relative POSIX paths of folders holding a dbt_project.yml ('.' for the root)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        if "dbt_project.yml" in filenames:
            rel = Path(dirpath).relative_to(root).as_posix()
            found.append(rel or ".")
    return found


def select_project(root: Path, project_dir: str | None) -> str:
    projects = find_dbt_projects(root)
    if project_dir is not None:
        wanted = PurePosixPath(project_dir.replace("\\", "/")).as_posix().strip("/") or "."
        if wanted not in projects:
            listed = ", ".join(projects) or "none"
            raise RepoError(f"repo.project_dir {project_dir!r} has no dbt_project.yml (projects found: {listed})")
        return wanted
    if not projects:
        raise RepoError("No dbt_project.yml found in the repo")
    if len(projects) > 1:
        raise RepoError(f"Several dbt projects in the repo ({', '.join(projects)}): set repo.project_dir")
    return projects[0]
