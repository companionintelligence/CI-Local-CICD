"""Install a ``pre-push`` hook that runs the local checks before code leaves.

Gating the hosted workflows only saves money if the checks still happen. This
hook is where they move to. It is deliberately escapable - ``SKIP_LOCAL_CI=1``
or ``git push --no-verify`` - because a hook that cannot be bypassed gets
deleted by whoever it blocks first.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

HOOK_NAME = "pre-push"
SENTINEL = "# managed-by: ci-local"

# `--repo` precedes the subcommand: it is a global flag, and the whole point of
# the hook is that it runs *this* repo's checks. Getting that wrong scoped every
# push to all 33 workspace repos - 37 minutes of unrelated, mostly-red output
# instead of 26 seconds - which teaches people to reach for SKIP_LOCAL_CI=1.
# `ci-local` now also accepts `--repo` after the subcommand, so both orders
# work, but the canonical global-first form is what gets written here.
#
# `--path` is the other half of "this repo". The hook file lives in the git dir
# that every worktree shares, and `--repo` alone resolves against the workspace
# - so a push from `CI-Capture/.claude/worktrees/x` was gated on the main
# clone's checkout: a red `main` rejected a green branch, and a green `main`
# would pass a broken one. git starts the hook at the pushing worktree's root,
# so that root is what gets checked; `--repo` still names the policy entry.

HOOK_TEMPLATE = """#!/usr/bin/env bash
{sentinel}
# Runs this repo's checks locally instead of on billed GitHub-hosted runners.
# Bypass with SKIP_LOCAL_CI=1 or `git push --no-verify`.
set -uo pipefail

if [ "${{SKIP_LOCAL_CI:-0}}" = "1" ]; then
  echo "local-ci: skipped (SKIP_LOCAL_CI=1)"
  exit 0
fi

CI_LOCAL="{cli}"
if [ ! -x "$CI_LOCAL" ] && [ ! -f "$CI_LOCAL" ]; then
  echo "local-ci: {cli} not found - skipping" >&2
  exit 0
fi

# The tree being pushed: every worktree runs this same file, from its own root.
REPO_PATH="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

echo "local-ci: running {checks} for {repo} in $REPO_PATH"
"{python}" "$CI_LOCAL" --repo "{repo}" run --path "$REPO_PATH" --only "{checks}" --quiet
status=$?
if [ $status -ne 0 ]; then
  echo "" >&2
  echo "local-ci: checks failed - push aborted." >&2
  echo "          re-run with: {python} $CI_LOCAL --repo {repo} run --path '$REPO_PATH'" >&2
  echo "          bypass with: SKIP_LOCAL_CI=1 git push" >&2
fi
exit $status
"""


@dataclass
class HookResult:
    repo: str
    action: str  # "installed" | "updated" | "skipped" | "removed"
    detail: str = ""


def git_dir(repo_path: Path) -> Optional[Path]:
    """Resolve ``.git`` for both normal clones and worktrees."""
    marker = repo_path / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        # Worktrees store `gitdir: /abs/path` in a plain file.
        try:
            content = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if content.startswith("gitdir:"):
            resolved = Path(content.split(":", 1)[1].strip())
            if not resolved.is_absolute():
                resolved = (repo_path / resolved).resolve()
            return resolved
    return None


def render_hook(repo: str, cli_path: Path, python: str, checks: List[str]) -> str:
    return HOOK_TEMPLATE.format(
        sentinel=SENTINEL,
        cli=cli_path,
        python=python,
        repo=repo,
        checks=",".join(checks),
    )


def install(
    repo_path: Path,
    cli_path: Path,
    python: str = "python3",
    checks: Optional[List[str]] = None,
    force: bool = False,
    apply: bool = False,
) -> HookResult:
    """Write the hook, backing up anything we did not write ourselves.

    The action is decided before anything is touched, so without ``apply`` the
    result is exactly what ``apply`` would report and nothing is written - not
    the hook, not the backup, not even the hooks dir. ``hooks install`` used to
    write unconditionally, which rewrote the hook in every workspace repo for
    anyone who ran it expecting the usual dry run.
    """
    repo = repo_path.name
    gitdir = git_dir(repo_path)
    if gitdir is None:
        return HookResult(repo, "skipped", "not a git repository")

    hooks_dir = gitdir / "hooks"
    hook_path = hooks_dir / HOOK_NAME
    backup = hooks_dir / f"{HOOK_NAME}.pre-ci-local"
    body = render_hook(repo, cli_path, python, checks or ["lint", "typecheck", "test"])

    action = "installed"
    displace = False
    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8", errors="replace")
        if SENTINEL in existing:
            if existing == body:
                return HookResult(repo, "skipped", "already current")
            action = "updated"
        elif force:
            if backup.exists():
                return HookResult(
                    repo, "skipped", f"backup already exists at {backup}"
                )
            action = "updated"
            displace = True
        else:
            return HookResult(
                repo, "skipped", "existing unmanaged hook (use --force)"
            )

    result = HookResult(repo, action, str(hook_path))
    if not apply:
        return result

    hooks_dir.mkdir(parents=True, exist_ok=True)
    if displace:
        hook_path.rename(backup)
    hook_path.write_text(body, encoding="utf-8")
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return result


def uninstall(repo_path: Path, apply: bool = False) -> HookResult:
    """Remove our hook and restore any hook we displaced.

    As with :func:`install`, nothing changes on disk without ``apply``.
    """
    repo = repo_path.name
    gitdir = git_dir(repo_path)
    if gitdir is None:
        return HookResult(repo, "skipped", "not a git repository")

    hook_path = gitdir / "hooks" / HOOK_NAME
    if not hook_path.exists():
        return HookResult(repo, "skipped", "no hook installed")
    if SENTINEL not in hook_path.read_text(encoding="utf-8", errors="replace"):
        return HookResult(repo, "skipped", "hook not managed by ci-local")

    backup = gitdir / "hooks" / f"{HOOK_NAME}.pre-ci-local"
    restore = backup.exists()
    result = HookResult(
        repo, "removed", "restored previous hook" if restore else str(hook_path)
    )
    if not apply:
        return result

    hook_path.unlink()
    if restore:
        backup.rename(hook_path)
    return result
