"""Workspace: the app's working copy of one repo commit.

The overlay (generated profile, shim package, patched yml) is applied here in
later phases, so the customer repo and the cache clone stay untouched.
Rebuilt from scratch on every run: the same commit + config gives the same files.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .repo import Checkout, repo_slug

_IGNORE = shutil.ignore_patterns(
    ".git", "target", "dbt_packages", "logs", ".user.yml", "__pycache__", ".venv",
    "*.duckdb", "*.duckdb.wal", ".env")

METADATA_DIR = ".dbt_horizon"


@dataclass(frozen=True)
class Workspace:
    root: Path            # copy of the repo
    project_dir: str      # repo-relative folder of dbt_project.yml
    checkout: Checkout

    @property
    def project_path(self) -> Path:
        return self.root / self.project_dir

    @property
    def metadata_path(self) -> Path:
        return self.root / METADATA_DIR


def remove_tree(path: Path) -> None:
    """rmtree that also works past Windows' 260-character limit: dbt writes compiled files
    with the \\\\?\\ long-path prefix (target/compiled/<project>/models/.../<test>.sql), so
    deleting them needs it too."""
    target = str(Path(path).resolve())
    if os.name == "nt" and not target.startswith("\\\\?\\"):
        target = "\\\\?\\" + target
    shutil.rmtree(target)


def create_workspace(checkout: Checkout, project_dir: str, cache_root: Path) -> Workspace:
    root = Path(cache_root) / "workspaces" / repo_slug(checkout.url) / checkout.commit[:12]
    if root.exists():
        remove_tree(root)
    # Whole repo, not only the project: packages.yml may use local paths like ../shared.
    shutil.copytree(checkout.path, root, ignore=_IGNORE)
    ws = Workspace(root=root, project_dir=project_dir, checkout=checkout)
    ws.metadata_path.mkdir(exist_ok=True)
    (ws.metadata_path / "workspace.json").write_text(json.dumps({
        "repo_url": checkout.url,
        "ref": checkout.ref,
        "commit": checkout.commit,
        "project_dir": project_dir,
    }, indent=2) + "\n", encoding="utf-8")
    return ws
