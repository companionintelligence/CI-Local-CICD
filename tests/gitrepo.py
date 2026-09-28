"""Real git repositories and worktrees for tests, sealed off from the host.

These tests may themselves run inside ci-local's pre-push hook, where a push
from a worktree has exported ``GIT_DIR``; left in place, every command here
would act on the real repository instead of the fixture. Global and system
config are dropped too, so a developer's ``core.hooksPath`` or
``init.defaultBranch`` cannot change what a test sees.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from local_ci.detect import git_env

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def sealed_env() -> dict:
    env = git_env()
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    })
    return env


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=sealed_env(), capture_output=True, text=True, check=check,
    )


def commit(repo: Path, message: str, *files: str) -> None:
    # Named files, never `add -A`: from the main clone that would sweep the
    # worktree nested under it into the index as an embedded repo.
    if files:
        git(repo, "add", "--", *files)
    git(repo, "commit", "-q", "--allow-empty", "-m", message)


@dataclass
class Checkouts:
    """A main clone, a linked worktree under it, and a bare remote."""

    main: Path
    worktree: Path
    remote: Path


def make_checkouts(root: Path, name: str = "Demo") -> Checkouts:
    """``<root>/<name>`` plus ``<name>/.claude/worktrees/feature``.

    The worktree sits where agent sessions put theirs, inside the main clone -
    the layout the pre-push hook got wrong.
    """
    remote = root / "remote.git"
    main = root / name
    main.mkdir(parents=True)
    git(root, "init", "-q", "--bare", str(remote))
    git(main, "init", "-q", "-b", "main")
    commit(main, "init")
    git(main, "remote", "add", "origin", str(remote))
    worktree = main / ".claude" / "worktrees" / "feature"
    git(main, "worktree", "add", "-q", "-b", "feature", str(worktree))
    # `resolve()`: git reports real paths, and macOS tmp dirs sit behind a symlink.
    return Checkouts(main.resolve(), worktree.resolve(), remote.resolve())
