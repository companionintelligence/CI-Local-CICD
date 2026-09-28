"""Pre-push hook installation, backup and removal."""

from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path

import pytest

from local_ci.cli import REPO_ROOT, build_parser
from local_ci.hooks import HOOK_NAME, SENTINEL, git_dir, install, render_hook, uninstall

from .gitrepo import commit, git, make_checkouts, requires_git
from .test_cli import green_policy

CLI = Path("/opt/ci/ci-local")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "CI-Server"
    (root / ".git" / "hooks").mkdir(parents=True)
    return root


def hook_file(repo):
    return repo / ".git" / "hooks" / HOOK_NAME


class TestGitDir:
    def test_normal_clone(self, repo):
        assert git_dir(repo) == repo / ".git"

    def test_worktree_gitdir_file(self, tmp_path):
        real = tmp_path / "main" / ".git" / "worktrees" / "wt"
        real.mkdir(parents=True)
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")
        assert git_dir(wt) == real

    def test_not_a_repo(self, tmp_path):
        assert git_dir(tmp_path) is None


class TestInstall:
    def test_writes_an_executable_hook(self, repo):
        result = install(repo, CLI)
        assert result.action == "installed"
        path = hook_file(repo)
        assert path.exists()
        assert os.stat(path).st_mode & stat.S_IXUSR

    def test_hook_carries_the_sentinel_and_repo_name(self, repo):
        install(repo, CLI)
        body = hook_file(repo).read_text(encoding="utf-8")
        assert SENTINEL in body
        assert "CI-Server" in body
        assert str(CLI) in body

    def test_hook_is_bypassable(self, repo):
        install(repo, CLI)
        body = hook_file(repo).read_text(encoding="utf-8")
        assert "SKIP_LOCAL_CI" in body
        assert "--no-verify" in body

    def test_reinstall_is_idempotent(self, repo):
        install(repo, CLI)
        result = install(repo, CLI)
        assert result.action == "skipped"
        assert result.detail == "already current"

    def test_changed_checks_update_our_own_hook(self, repo):
        install(repo, CLI, checks=["test"])
        result = install(repo, CLI, checks=["lint", "test"])
        assert result.action == "updated"
        assert "lint,test" in hook_file(repo).read_text(encoding="utf-8")

    def test_unmanaged_hook_is_not_clobbered(self, repo):
        hook_file(repo).write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        result = install(repo, CLI)
        assert result.action == "skipped"
        assert "--force" in result.detail
        assert "echo mine" in hook_file(repo).read_text(encoding="utf-8")

    def test_force_backs_up_the_existing_hook(self, repo):
        hook_file(repo).write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        result = install(repo, CLI, force=True)
        assert result.action == "updated"
        backup = repo / ".git" / "hooks" / f"{HOOK_NAME}.pre-ci-local"
        assert "echo mine" in backup.read_text(encoding="utf-8")
        assert SENTINEL in hook_file(repo).read_text(encoding="utf-8")

    def test_force_refuses_to_overwrite_an_existing_backup(self, repo):
        (repo / ".git" / "hooks" / f"{HOOK_NAME}.pre-ci-local").write_text(
            "original\n", encoding="utf-8"
        )
        hook_file(repo).write_text("#!/bin/sh\necho second\n", encoding="utf-8")
        result = install(repo, CLI, force=True)
        assert result.action == "skipped"
        assert "backup already exists" in result.detail

    def test_non_repo_is_skipped(self, tmp_path):
        assert install(tmp_path, CLI).action == "skipped"

    def test_hooks_dir_is_created_when_absent(self, tmp_path):
        root = tmp_path / "repo"
        (root / ".git").mkdir(parents=True)
        assert install(root, CLI).action == "installed"


class TestUninstall:
    def test_removes_our_hook(self, repo):
        install(repo, CLI)
        result = uninstall(repo)
        assert result.action == "removed"
        assert not hook_file(repo).exists()

    def test_restores_a_backed_up_hook(self, repo):
        hook_file(repo).write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        install(repo, CLI, force=True)
        uninstall(repo)
        assert "echo mine" in hook_file(repo).read_text(encoding="utf-8")

    def test_leaves_unmanaged_hooks_alone(self, repo):
        hook_file(repo).write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        result = uninstall(repo)
        assert result.action == "skipped"
        assert hook_file(repo).exists()

    def test_no_hook_installed(self, repo):
        assert uninstall(repo).action == "skipped"


def test_rendered_hook_is_valid_shell():
    import subprocess

    body = render_hook("CI-Server", CLI, "python3", ["lint", "test"])
    proc = subprocess.run(["bash", "-n"], input=body, text=True,
                          capture_output=True)
    assert proc.returncode == 0, proc.stderr


def ci_local_argv(body: str) -> list:
    """The arguments the hook actually hands to ``ci-local``."""
    # The invocation is the only line where "$CI_LOCAL" is followed by flags;
    # the existence guard has `"$CI_LOCAL" ]` and the hint echoes it unquoted.
    line = next(l for l in body.splitlines() if '"$CI_LOCAL" --' in l)
    return shlex.split(line.split('"$CI_LOCAL"', 1)[1])


def test_hook_command_line_scopes_to_this_repo():
    """Parse the hook's own command line with the real parser.

    This is the seam that broke: the hook body is a string in one module and
    the flags it uses are defined in another, so nothing checked that they
    meant what the hook intended. They did not - ``--repo`` sat after the
    subcommand, where argparse absorbed it as an abbreviation of ``--report``,
    leaving the run unscoped across every repo in the workspace.
    """
    argv = ci_local_argv(render_hook("CI-Server", CLI, "python3", ["lint", "test"]))
    args = build_parser().parse_args(argv)
    assert args.command == "run"
    assert args.repo == ["CI-Server"]
    assert args.path == Path("$REPO_PATH")  # the pushing tree, not the workspace's
    assert args.only == "lint,test"
    assert args.report is None  # not misread as a report path
    assert args.quiet is True


def test_failure_hint_is_a_command_that_parses():
    """The 're-run with:' line is the one people copy after a red push."""
    body = render_hook("CI-Server", CLI, "python3", ["lint", "test"])
    hint = next(l for l in body.splitlines() if "re-run with:" in l)
    argv = shlex.split(hint.split("$CI_LOCAL", 1)[1].rstrip('" >&2'))
    args = build_parser().parse_args(argv)
    assert args.repo == ["CI-Server"]
    assert args.command == "run"
    assert args.path == Path("$REPO_PATH")


@requires_git
class TestWorktreePush:
    """A real ``git push`` through the installed hook, from a real worktree.

    Hooks live in the git dir every worktree shares, and the hook used to name
    only ``--repo`` - which resolves against the workspace - so a push from
    ``.claude/worktrees/<x>`` was gated on whatever the main clone had checked
    out. Observed 2026-09-27: a CI-Capture branch that passed fmt, clippy and
    tests was rejected because the main clone, on ``main``, failed fmt.
    """

    def _arm(self, tmp_path):
        co = make_checkouts(tmp_path / "ws")
        policy = green_policy(tmp_path / "policy.toml")
        # The hook runs the real CLI. The shim only aims it at this workspace
        # and policy, where a `--repo`-only lookup finds the main clone exactly
        # as it does in the real workspace.
        shim = tmp_path / "ci-local"
        shim.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from local_ci.cli import main\n"
            f"raise SystemExit(main(['--workspace', {str(co.main.parent)!r}, "
            f"'--policy', {str(policy)!r}, *sys.argv[1:]]))\n",
            encoding="utf-8",
        )
        assert install(co.main, shim, sys.executable, ["test"]).action == "installed"
        return co

    @staticmethod
    def _push(checkout, branch):
        proc = git(checkout, "push", "-q", "origin", branch, check=False)
        return proc.returncode, proc.stdout + proc.stderr

    def test_green_branch_is_not_blocked_by_a_red_main_clone(self, tmp_path):
        co = self._arm(tmp_path)
        (co.worktree / "GREEN").write_text("", encoding="utf-8")
        commit(co.worktree, "go green", "GREEN")

        code, out = self._push(co.worktree, "feature")
        assert code == 0, out
        assert f"in {co.worktree}" in out

    def test_red_branch_is_not_waved_through_by_a_green_main_clone(self, tmp_path):
        co = self._arm(tmp_path)
        (co.main / "GREEN").write_text("", encoding="utf-8")
        commit(co.main, "main is green", "GREEN")  # the worktree's branch is not

        code, out = self._push(co.worktree, "feature")
        assert code != 0, out
        assert "push aborted" in out
        assert f"--path '{co.worktree}'" in out  # the re-run hint targets it too

    def test_main_clone_push_still_checks_the_main_clone(self, tmp_path):
        co = self._arm(tmp_path)
        (co.main / "GREEN").write_text("", encoding="utf-8")
        commit(co.main, "main is green", "GREEN")

        code, out = self._push(co.main, "main")
        assert code == 0, out
        assert f"in {co.main}\n" in out
