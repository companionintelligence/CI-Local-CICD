"""The pre-push hook git actually runs, when core.hooksPath moves it.

With core.hooksPath set - husky's `.husky/_`, a `.githooks` directory - git
never reads `.git/hooks`, so a ci-local hook there is dead: pushes go through
without running a single check. These tests use real repositories, and the
push tests push through the hook git picks.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from local_ci import cli
from local_ci.cli import REPO_ROOT, main
from local_ci.hooks import (
    BLOCK_BEGIN, BLOCK_END, CREATED, HOOK_NAME, SENTINEL, _temp_dirs, hook_dirs, install,
    render_block, render_hook, status, uninstall, unstable_location,
)

from .gitrepo import commit, git, make_checkouts, requires_git
from .test_cli import green_policy

pytestmark = requires_git

CLI = Path("/opt/ci/ci-local")

# What husky 9 leaves in `.husky/_/pre-push`: no trailing newline, and it
# hands over to `h`, which runs the user's `.husky/pre-push` and exits.
HUSKY_WRAPPER = '#!/usr/bin/env sh\n. "$(dirname "$0")/h"'
HUSKY_H = (
    '#!/usr/bin/env sh\n'
    'user="$(dirname "$(dirname "$0")")/$(basename "$0")"\n'
    '[ -f "$user" ] || exit 0\n'
    'sh -e "$user" "$@"\n'
    'exit $?\n'
)


@pytest.fixture(autouse=True)
def sealed_git(monkeypatch):
    """hooks asks git where hooks live; keep this machine's git config out of it."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("SKIP_LOCAL_CI", raising=False)


def executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def husky(repo: Path) -> Path:
    """Set ``repo`` up the way husky 9 does: an ignored `.husky/_` of wrappers."""
    underscore = repo / ".husky" / "_"
    underscore.mkdir(parents=True)
    (underscore / ".gitignore").write_text("*", encoding="utf-8")
    (underscore / "h").write_text(HUSKY_H, encoding="utf-8")
    executable(underscore / HOOK_NAME, HUSKY_WRAPPER)
    git(repo, "config", "core.hooksPath", ".husky/_")
    return underscore / HOOK_NAME


def githooks(repo: Path, tracked: bool = True) -> Path:
    """A `.githooks` directory whose pre-push runs the repo's own gate."""
    hook = executable(repo / ".githooks" / HOOK_NAME,
                      '#!/bin/sh\nset -e\necho "[pre-push] repo gate"\n')
    if tracked:
        commit(repo, "add hooks", ".githooks/pre-push")
    git(repo, "config", "core.hooksPath", ".githooks")
    return hook


def node_repo(root: Path, name: str) -> Path:
    """A clone ci-local detects checks in, so that `hooks install` offers it a hook."""
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "package.json").write_text(
        json.dumps({"name": name.lower(), "scripts": {"test": "exit 0"}}), encoding="utf-8")
    commit(repo, "init", "package.json")
    return repo


@pytest.fixture
def stable_cli(monkeypatch):
    """Let `hooks install --apply` run wherever this suite is checked out.

    The suite may run from a linked worktree or a temp dir, where the real
    command refuses to install; TestInstallLocation covers that refusal.
    """
    monkeypatch.setattr(cli, "unstable_location", lambda root: None)


def tree(root: Path) -> dict:
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode) if p.is_file() else None
        for p in sorted(root.rglob("*"))
    }


@pytest.fixture
def co(tmp_path):
    return make_checkouts(tmp_path / "ws")


class TestHookDirs:
    def test_plain_repo_runs_git_hooks(self, co):
        dirs = hook_dirs(co.main)
        assert dirs.active == dirs.own == co.main / ".git" / "hooks"
        assert not dirs.redirected

    def test_a_linked_worktree_shares_the_main_clones_hooks(self, co):
        assert hook_dirs(co.worktree).own == co.main / ".git" / "hooks"
        assert not hook_dirs(co.worktree).redirected

    def test_a_linked_worktree_knows_its_main_clone(self, co):
        assert hook_dirs(co.worktree).main_clone == co.main
        assert hook_dirs(co.main).main_clone is None

    def test_relative_hooks_path_is_per_worktree(self, co):
        husky(co.main)
        dirs = hook_dirs(co.main)
        assert dirs.active == co.main / ".husky" / "_"
        assert dirs.redirected and dirs.per_worktree
        assert dirs.configured == ".husky/_"
        # The same setting, read from the linked worktree, names its own copy.
        assert hook_dirs(co.worktree).active == co.worktree / ".husky" / "_"

    def test_absolute_hooks_path_is_shared(self, co):
        git(co.main, "config", "core.hooksPath", str(co.main / ".githooks"))
        dirs = hook_dirs(co.main)
        assert dirs.active == co.main / ".githooks"
        assert dirs.redirected and not dirs.per_worktree

    def test_a_directory_git_does_not_recognise_falls_back(self, tmp_path):
        root = tmp_path / "fixture"
        (root / ".git" / "hooks").mkdir(parents=True)
        dirs = hook_dirs(root)
        assert dirs.active == dirs.own == root / ".git" / "hooks"

    def test_not_a_repo(self, tmp_path):
        assert hook_dirs(tmp_path) is None


class TestInstallIntoHooksPath:
    def test_the_block_goes_first_in_the_hook_git_runs(self, co):
        wrapper = husky(co.main)
        result = install(co.main, CLI, apply=True)
        assert result.action == "installed"
        assert str(wrapper) in result.detail
        text = wrapper.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env sh\n" + BLOCK_BEGIN)
        assert text.endswith(BLOCK_END + "\n" + '. "$(dirname "$0")/h"')
        assert SENTINEL not in text  # a shared file is never "owned"
        assert not (co.main / ".git" / "hooks" / HOOK_NAME).exists()

    def test_a_missing_hook_file_is_created_executable(self, co):
        husky(co.main).unlink()
        install(co.main, CLI, apply=True)
        hook = co.main / ".husky" / "_" / HOOK_NAME
        assert hook.read_text(encoding="utf-8").startswith("#!/bin/sh\n" + BLOCK_BEGIN)
        assert os.access(hook, os.X_OK)

    def test_reinstall_is_idempotent_and_updates_in_place(self, co):
        wrapper = husky(co.main)
        install(co.main, CLI, checks=["test"], apply=True)
        assert install(co.main, CLI, checks=["test"], apply=True).detail == "already current"
        result = install(co.main, CLI, checks=["lint", "test"], apply=True)
        assert result.action == "updated"
        text = wrapper.read_text(encoding="utf-8")
        assert text.count(BLOCK_BEGIN) == 1
        assert '--only "lint,test"' in text
        assert text.endswith('. "$(dirname "$0")/h"')

    def test_the_dead_copy_in_git_hooks_is_retired(self, co):
        install(co.main, CLI, apply=True)  # before husky: .git/hooks/pre-push
        husky(co.main)
        result = install(co.main, CLI, apply=True)
        assert "removes the copy in" in result.detail
        assert not (co.main / ".git" / "hooks" / HOOK_NAME).exists()

    def test_a_hook_an_older_ci_local_moved_aside_is_put_back(self, co):
        own = co.main / ".git" / "hooks" / HOOK_NAME
        # What an older --force left: the other hook moved aside, ci-local's in place.
        executable(own.with_name(f"{HOOK_NAME}.pre-ci-local"), "#!/bin/sh\necho mine\n")
        install(co.main, CLI, apply=True)
        husky(co.main)
        install(co.main, CLI, apply=True)
        assert own.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"

    def test_relative_hooks_path_says_worktrees_are_not_covered(self, co):
        husky(co.main)
        assert "each linked worktree resolves core.hooksPath to its own copy" in \
            install(co.main, CLI).detail

    def test_a_tracked_hook_file_needs_edit_tracked(self, co):
        hook = githooks(co.main)
        original = hook.read_text(encoding="utf-8")
        result = install(co.main, CLI, apply=True)
        assert result.action == "skipped"
        assert result.detail == (f"{hook} is tracked by git: the block would show as a "
                                 f"local change (to edit it anyway: --repo Demo --edit-tracked)")
        # --force is for .git/hooks; it does not reach a tracked file.
        assert install(co.main, CLI, force=True, apply=True).action == "skipped"
        assert hook.read_text(encoding="utf-8") == original

        preview = install(co.main, CLI, edit_tracked=True)
        assert "tracked by git: the block will show as a local change" in preview.detail
        assert install(co.main, CLI, edit_tracked=True, apply=True) == preview
        text = hook.read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh\n" + BLOCK_BEGIN)
        assert text.endswith(original.split("\n", 1)[1])

    def test_a_file_git_would_commit_needs_edit_tracked(self, co):
        (co.main / ".githooks").mkdir()
        git(co.main, "config", "core.hooksPath", ".githooks")
        result = install(co.main, CLI, force=True, apply=True)
        assert "not ignored by git: the block could be committed" in result.detail
        assert not (co.main / ".githooks" / HOOK_NAME).exists()
        result = install(co.main, CLI, edit_tracked=True, apply=True)
        assert result.action == "installed"
        assert "not ignored by git: the block could be committed" in result.detail

    def test_a_hooks_path_inside_the_git_dir_is_fine(self, co):
        (co.main / ".git" / "custom-hooks").mkdir()
        git(co.main, "config", "core.hooksPath", ".git/custom-hooks")
        assert install(co.main, CLI, apply=True).action == "installed"

    def test_a_hooks_path_outside_the_repo_is_refused_even_with_both_flags(self, co, tmp_path):
        shared = tmp_path / "shared-hooks"
        shared.mkdir()
        git(co.main, "config", "core.hooksPath", str(shared))
        result = install(co.main, CLI, force=True, edit_tracked=True, apply=True)
        assert result.action == "skipped"
        assert "outside this repo" in result.detail
        assert list(shared.iterdir()) == []

    def test_a_missing_hooks_path_directory_is_reported(self, co):
        git(co.main, "config", "core.hooksPath", ".husky/_")
        result = install(co.main, CLI, apply=True)
        assert result.action == "skipped"
        assert "does not exist, so git runs no pre-push hook" in result.detail

    def test_a_hook_git_will_not_execute_is_left_alone(self, co):
        wrapper = husky(co.main)
        wrapper.chmod(0o644)
        result = install(co.main, CLI, apply=True)
        assert "not executable, so git does not run it" in result.detail
        assert wrapper.read_text(encoding="utf-8") == HUSKY_WRAPPER

    def test_a_damaged_block_is_not_guessed_at(self, co):
        wrapper = husky(co.main)
        executable(wrapper, f"#!/bin/sh\n{BLOCK_BEGIN}\necho half a block\n")
        result = install(co.main, CLI, apply=True)
        assert result.action == "skipped"
        assert "markers are missing or repeated" in result.detail

    @pytest.mark.parametrize("first_line, runs", [
        ("#!/bin/sh", True),
        ("#!/bin/sh -e", True),
        ("#!/usr/bin/env sh", True),
        ("#!/usr/bin/env -S bash -e", True),
        ("#!/usr/bin/env LC_ALL=C dash", True),
        ("#!/bin/zsh", True),
        ("#!/bin/ksh", True),
        ("echo no shebang, so git runs this with sh", True),
        ("#!/usr/bin/env python3", False),
        ("#!/usr/bin/env node", False),
        ("#!/usr/local/bin/ruby -w", False),
        ("#!", False),
    ])
    def test_only_a_shell_hook_gets_the_block(self, co, first_line, runs):
        """The block is sh; in a Python hook it is a syntax error on every push."""
        wrapper = husky(co.main)
        executable(wrapper, first_line + "\necho the rest\n")
        result = install(co.main, CLI, apply=True)
        if runs:
            assert result.action == "installed", result.detail
        else:
            assert result.action == "skipped"
            assert "not a POSIX shell, and the block is sh" in result.detail
            assert wrapper.read_text(encoding="utf-8") == first_line + "\necho the rest\n"

    def test_a_symlinked_hook_is_not_written_through(self, co, tmp_path):
        target = executable(tmp_path / "elsewhere" / "pre-push", "#!/bin/sh\n")
        wrapper = husky(co.main)
        wrapper.unlink()
        wrapper.symlink_to(target)
        assert "is a symlink" in install(co.main, CLI, apply=True).detail
        assert target.read_text(encoding="utf-8") == "#!/bin/sh\n"


def _with_dead_copy(repo):
    install(repo, CLI, apply=True)
    husky(repo)


LFS_HOOK = '#!/bin/sh\ngit lfs pre-push "$@"\n'


CHAINED_CASES = {
    "husky wrapper": husky,
    "husky, no pre-push yet": lambda repo: husky(repo).unlink(),
    "block current": lambda repo: (husky(repo), install(repo, CLI, apply=True)),
    "dead copy in .git/hooks": _with_dead_copy,
    "tracked .githooks": githooks,
    "untracked .githooks": lambda repo: githooks(repo, tracked=False),
    "git-lfs hook in .git/hooks": lambda repo: executable(
        repo / ".git" / "hooks" / HOOK_NAME, LFS_HOOK),
}


@pytest.mark.parametrize("case", CHAINED_CASES)
@pytest.mark.parametrize("flags", [{}, {"force": True}, {"edit_tracked": True}],
                         ids=["no flag", "force", "edit_tracked"])
def test_dry_run_writes_nothing_and_matches_apply(co, case, flags):
    CHAINED_CASES[case](co.main)
    before = tree(co.main)
    preview = install(co.main, CLI, **flags)
    assert tree(co.main) == before
    assert install(co.main, CLI, apply=True, **flags) == preview


class TestUninstall:
    def test_the_shared_file_goes_back_to_what_it_was(self, co):
        wrapper = husky(co.main)
        install(co.main, CLI, apply=True)
        result = uninstall(co.main, apply=True)
        assert result.action == "removed"
        assert wrapper.read_text(encoding="utf-8") == HUSKY_WRAPPER

    def test_a_tracked_file_is_left_unmodified(self, co):
        githooks(co.main)
        install(co.main, CLI, edit_tracked=True, apply=True)
        assert git(co.main, "status", "--porcelain", "--", ".githooks").stdout
        uninstall(co.main, apply=True)
        assert git(co.main, "status", "--porcelain", "--", ".githooks").stdout == ""

    @pytest.mark.parametrize("original", ["#!/bin/sh\n", ""], ids=["shebang only", "empty"])
    def test_a_hook_with_nothing_else_in_it_is_put_back(self, co, original):
        """Content alone cannot say ci-local created a file; the BEGIN line does."""
        hook = executable(co.main / ".githooks" / HOOK_NAME, original)
        commit(co.main, "add hooks", ".githooks/pre-push")
        git(co.main, "config", "core.hooksPath", ".githooks")
        install(co.main, CLI, edit_tracked=True, apply=True)
        assert "deleted" not in uninstall(co.main, apply=True).detail
        assert hook.read_text(encoding="utf-8") == original
        assert git(co.main, "status", "--porcelain", "--", ".githooks").stdout == ""

    def test_a_file_ci_local_created_is_deleted(self, co):
        husky(co.main).unlink()
        install(co.main, CLI, apply=True)
        assert "deleted" in uninstall(co.main, apply=True).detail
        assert not (co.main / ".husky" / "_" / HOOK_NAME).exists()

    def test_a_file_ci_local_created_stays_marked_across_reinstalls(self, co):
        husky(co.main).unlink()
        hook = co.main / ".husky" / "_" / HOOK_NAME
        install(co.main, CLI, checks=["test"], apply=True)
        assert install(co.main, CLI, checks=["lint"], apply=True).action == "updated"
        assert CREATED in hook.read_text(encoding="utf-8").splitlines()[1]
        assert install(co.main, CLI, checks=["lint"], apply=True).detail == "already current"
        assert "deleted" in uninstall(co.main, apply=True).detail

    def test_a_created_file_that_gained_lines_is_kept(self, co):
        husky(co.main).unlink()
        install(co.main, CLI, apply=True)
        hook = co.main / ".husky" / "_" / HOOK_NAME
        hook.write_text(hook.read_text(encoding="utf-8") + "echo added later\n", encoding="utf-8")
        assert "deleted" not in uninstall(co.main, apply=True).detail
        assert hook.read_text(encoding="utf-8") == "#!/bin/sh\necho added later\n"

    def test_the_dead_copy_goes_too(self, co):
        install(co.main, CLI, apply=True)
        husky(co.main)
        result = uninstall(co.main, apply=True)
        assert "which git does not run" in result.detail
        assert not (co.main / ".git" / "hooks" / HOOK_NAME).exists()

    def test_nothing_to_remove(self, co):
        husky(co.main)
        assert uninstall(co.main, apply=True).detail == "no hook installed"

    def test_dry_run_changes_nothing(self, co):
        husky(co.main)
        install(co.main, CLI, apply=True)
        before = tree(co.main)
        preview = uninstall(co.main)
        assert tree(co.main) == before
        assert uninstall(co.main, apply=True) == preview


class TestStatus:
    def test_no_hook(self, co):
        result = status(co.main, CLI)
        assert result.action == "none"
        assert str(co.main / ".git" / "hooks" / HOOK_NAME) in result.detail

    def test_installed_and_current(self, co):
        install(co.main, CLI, apply=True)
        result = status(co.main, CLI)
        assert result.action == "live"
        assert result.detail.endswith("(current)")

    def test_a_hook_from_an_older_ci_local_would_be_rewritten(self, co):
        # The earlier template let the push through when ci-local was missing.
        executable(co.main / ".git" / "hooks" / HOOK_NAME,
                   f"#!/usr/bin/env bash\n{SENTINEL}\necho 'not found - skipping'\nexit 0\n")
        result = status(co.main, CLI)
        assert result.action == "live"
        assert "hooks install would rewrite it" in result.detail

    def test_different_checks_would_be_rewritten(self, co):
        install(co.main, CLI, checks=["test"], apply=True)
        assert "would rewrite" in status(co.main, CLI).detail
        assert status(co.main, CLI, checks=["test"]).detail.endswith("(current)")

    def test_a_hook_in_git_hooks_is_dead_once_hooks_path_is_set(self, co):
        install(co.main, CLI, apply=True)
        husky(co.main)
        result = status(co.main, CLI)
        assert result.action == "dead"
        assert "core.hooksPath is .husky/_" in result.detail
        assert str(co.main / ".husky" / "_" / HOOK_NAME) in result.detail

    def test_reinstalling_makes_it_live(self, co):
        install(co.main, CLI, apply=True)
        husky(co.main)
        install(co.main, CLI, apply=True)
        assert status(co.main, CLI).action == "live"

    def test_husky_rewriting_its_wrapper_drops_the_block(self, co):
        wrapper = husky(co.main)
        install(co.main, CLI, apply=True)
        executable(wrapper, HUSKY_WRAPPER)  # what `husky` does on every install
        result = status(co.main, CLI)
        assert result.action == "none"
        assert result.detail.endswith("(core.hooksPath .husky/_)")

    def test_a_hook_that_is_not_executable_is_dead(self, co):
        install(co.main, CLI, apply=True)
        (co.main / ".git" / "hooks" / HOOK_NAME).chmod(0o644)
        result = status(co.main, CLI)
        assert result.action == "dead"
        assert "not executable" in result.detail

    def test_linked_worktrees_a_relative_hooks_path_leaves_out(self, co):
        husky(co.main)
        install(co.main, CLI, apply=True)
        result = status(co.main, CLI)
        assert result.action == "live"
        assert "1 of 1 linked worktree(s) resolve core.hooksPath to their own " \
               ".husky/_, which has no ci-local block" in result.detail

    def test_a_tracked_hook_with_the_block_is_compared_not_refused(self, co):
        githooks(co.main)
        install(co.main, CLI, edit_tracked=True, apply=True)
        assert f"{co.main / '.githooks' / HOOK_NAME} (current)" in status(co.main, CLI).detail

    def test_status_writes_nothing(self, co):
        install(co.main, CLI, apply=True)
        husky(co.main)
        before = tree(co.main)
        status(co.main, CLI)
        assert tree(co.main) == before

    def test_on_the_command_line(self, co, tmp_path, capsys):
        install(co.main, CLI, apply=True)
        husky(co.main)
        main(["--workspace", str(co.main.parent), "--policy", str(tmp_path / "none.toml"),
              "hooks", "status"])
        out = capsys.readouterr().out
        assert "  dead      Demo  " in out
        assert out.rstrip().endswith("1 dead.")

    def test_a_block_in_an_existing_git_hooks_hook_is_live(self, co):
        executable(co.main / ".git" / "hooks" / HOOK_NAME, LFS_HOOK)
        install(co.main, CLI, force=True, apply=True)
        result = status(co.main, CLI)
        assert result.action == "live"
        assert result.detail.endswith("(current)")


class TestLinkedWorktrees:
    """A linked worktree runs the main clone's hook, so only the main clone manages it."""

    LINKED = "linked worktree of {}; manage the hook from the main clone"

    def test_install_uninstall_and_status_leave_it_to_the_main_clone(self, co):
        results = [install(co.worktree, CLI, apply=True), uninstall(co.worktree, apply=True),
                   status(co.worktree, CLI)]
        assert {(r.action, r.detail) for r in results} == {("skipped", self.LINKED.format(co.main))}
        assert not (co.main / ".git" / "hooks" / HOOK_NAME).exists()
        assert not (co.main / ".git" / "worktrees" / "feature" / "hooks").exists()

    def test_the_main_clones_hook_is_not_renamed_from_a_worktree(self, co):
        install(co.main, CLI, apply=True)
        hook = co.main / ".git" / "hooks" / HOOK_NAME
        before = hook.read_bytes()
        assert install(co.worktree, CLI, checks=["lint"], apply=True).action == "skipped"
        assert uninstall(co.worktree, apply=True).action == "skipped"
        assert hook.read_bytes() == before

    def test_a_workspace_with_a_clone_and_its_worktree(self, tmp_path, capsys, stable_cli):
        """Both are workspace folders. The shared hook keeps the clone's name, and stays put."""
        ws = tmp_path / "ws"
        clone = node_repo(ws, "Main")
        git(clone, "worktree", "add", "-q", "-b", "feature", str(ws / "Main-feature"))
        argv = ["--workspace", str(ws), "--policy", str(tmp_path / "none.toml"),
                "hooks", "install", "--apply"]
        main(argv)
        out = capsys.readouterr().out
        assert "  installed Main  " in out
        assert f"  skipped   Main-feature  {self.LINKED.format(clone.resolve())}" in out
        hook = clone / ".git" / "hooks" / HOOK_NAME
        assert '--repo "Main" ' in hook.read_text(encoding="utf-8")
        main(argv)
        assert "  skipped   Main  already current" in capsys.readouterr().out
        assert '--repo "Main" ' in hook.read_text(encoding="utf-8")


class TestInstallLocation:
    """The hook names the ci-local that installed it, and refuses pushes without it."""

    @pytest.fixture
    def argv(self, co, tmp_path):
        policy = green_policy(tmp_path / "policy.toml")
        return ["--workspace", str(co.main.parent), "--policy", str(policy), "hooks", "install"]

    def test_a_linked_worktree_is_not_a_place_to_install_from(self, co):
        assert unstable_location(co.worktree, temp_dirs=[]) == \
            f"{co.worktree} is a linked worktree of {co.main}"

    def test_nor_is_a_temp_directory(self, co, tmp_path):
        temp = tmp_path.resolve()
        assert unstable_location(co.main, temp_dirs=[temp]) == \
            f"{co.main} is under the temporary directory {temp}"

    def test_a_main_clone_outside_them_is(self, co):
        assert unstable_location(co.main, temp_dirs=[]) is None

    def test_the_system_temp_directory_counts(self):
        assert Path(tempfile.gettempdir()).resolve() in _temp_dirs()

    def test_apply_refuses_and_writes_nothing(self, co, argv, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", co.worktree)
        with pytest.raises(SystemExit, match="not installing: .* is a linked worktree of"):
            main(argv + ["--apply"])
        assert not (co.main / ".git" / "hooks" / HOOK_NAME).exists()

    def test_the_dry_run_warns_and_names_what_the_hooks_run(self, co, argv, monkeypatch, capsys):
        monkeypatch.setattr(cli, "REPO_ROOT", co.worktree)
        assert main(argv) == 0
        out, err = capsys.readouterr()
        assert "warning: --apply will refuse:" in err and "--allow-temporary-cli" in err
        assert f"hooks run: {sys.executable} {co.worktree / 'ci-local'}" in out

    def test_the_flag_installs_anyway(self, co, argv, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", co.worktree)
        assert main(argv + ["--apply", "--allow-temporary-cli"]) == 0
        hook = co.main / ".git" / "hooks" / HOOK_NAME
        assert f'CI_LOCAL="{co.worktree / "ci-local"}"' in hook.read_text(encoding="utf-8")

    def test_edit_tracked_needs_repo(self, argv):
        with pytest.raises(SystemExit, match="name each repo with --repo"):
            main(argv + ["--edit-tracked"])


def test_a_fleet_wide_force_run(tmp_path, capsys, stable_cli):
    """--force reaches .git/hooks only: git-lfs keeps running, tracked files stay as they are."""
    ws = tmp_path / "ws"
    lfs = node_repo(ws, "Lfs")
    lfs_hook = executable(lfs / ".git" / "hooks" / HOOK_NAME, LFS_HOOK)
    tracked = node_repo(ws, "Tracked")
    githooks(tracked)
    main(["--workspace", str(ws), "--policy", str(tmp_path / "none.toml"),
          "hooks", "install", "--force", "--apply"])
    out = capsys.readouterr().out
    assert "  installed Lfs  " in out
    assert lfs_hook.read_text(encoding="utf-8").endswith(f'{BLOCK_END}\ngit lfs pre-push "$@"\n')
    assert "  skipped   Tracked  " in out and "--repo Tracked --edit-tracked" in out
    assert git(tracked, "status", "--porcelain").stdout == ""


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
class TestPushes:
    """Real pushes, through whichever hook git picks."""

    def _shim(self, tmp_path, co) -> Path:
        policy = green_policy(tmp_path / "policy.toml")
        shim = tmp_path / "ci-local"
        shim.write_text(
            "import sys\n"
            # Reads stdin as a tool might. The block gives it none, so the
            # pushed refs still reach the rest of the hook.
            "sys.stdin.read()\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from local_ci.cli import main\n"
            f"raise SystemExit(main(['--workspace', {str(co.main.parent)!r}, "
            f"'--policy', {str(policy)!r}, *sys.argv[1:]]))\n",
            encoding="utf-8",
        )
        return shim

    def _user_hook(self, repo, tmp_path) -> Path:
        """The repo's own husky pre-push, which records that it ran and its stdin."""
        record = tmp_path / "user-hook-ran"
        executable(repo / ".husky" / HOOK_NAME, f'cat > "{record}"\n')
        return record

    @staticmethod
    def _push(checkout, branch="main"):
        proc = git(checkout, "push", "-q", "origin", branch, check=False)
        return proc.returncode, proc.stdout + proc.stderr

    def test_a_hook_in_git_hooks_never_ran_under_husky(self, co, tmp_path):
        """How the fleet was: installed, red, and the push went through anyway."""
        install(co.main, self._shim(tmp_path, co), sys.executable, ["test"], apply=True)
        husky(co.main)
        code, out = self._push(co.main)  # no GREEN file: the check would fail
        assert code == 0, out
        assert "local-ci" not in out

    def test_the_chained_block_blocks_a_red_push(self, co, tmp_path):
        husky(co.main)
        record = self._user_hook(co.main, tmp_path)
        install(co.main, self._shim(tmp_path, co), sys.executable, ["test"], apply=True)
        code, out = self._push(co.main)
        assert code != 0, out
        assert "push aborted" in out
        assert not record.exists()  # the push stopped before the repo's own hook

    def test_a_green_push_runs_both_gates_and_keeps_stdin(self, co, tmp_path):
        husky(co.main)
        record = self._user_hook(co.main, tmp_path)
        install(co.main, self._shim(tmp_path, co), sys.executable, ["test"], apply=True)
        (co.main / "GREEN").write_text("", encoding="utf-8")
        commit(co.main, "green", "GREEN")
        code, out = self._push(co.main)
        assert code == 0, out
        assert f"local-ci: running test for Demo in {co.main}" in out
        # git's ref list reached the repo's own hook, unread by the block.
        assert record.read_text(encoding="utf-8").startswith("refs/heads/main ")

    def test_a_missing_ci_local_refuses_the_push(self, co, tmp_path):
        install(co.main, tmp_path / "gone" / "ci-local", sys.executable, apply=True)
        code, out = self._push(co.main)
        assert code != 0, out
        assert "not found - push aborted" in out

    def test_a_missing_python_refuses_the_push(self, co, tmp_path):
        install(co.main, self._shim(tmp_path, co), str(tmp_path / "no-python"), apply=True)
        code, out = self._push(co.main)
        assert code != 0, out
        assert "no-python not found - push aborted" in out

    def test_skip_local_ci_still_runs_the_rest_of_the_hook(self, co, tmp_path, monkeypatch):
        husky(co.main)
        record = self._user_hook(co.main, tmp_path)
        install(co.main, tmp_path / "gone" / "ci-local", sys.executable, apply=True)
        monkeypatch.setenv("SKIP_LOCAL_CI", "1")
        code, out = self._push(co.main)
        assert code == 0, out
        assert "skipped (SKIP_LOCAL_CI=1)" in out
        assert record.exists()

    def test_an_existing_git_hooks_hook_runs_after_the_block(self, co, tmp_path):
        """A git-lfs hook in .git/hooks: --force adds the block, and LFS still gets the refs."""
        record = tmp_path / "lfs-ran"
        executable(co.main / ".git" / "hooks" / HOOK_NAME, f'#!/bin/sh\ncat > "{record}"\n')
        install(co.main, self._shim(tmp_path, co), sys.executable, ["test"],
                force=True, apply=True)
        code, out = self._push(co.main)
        assert code != 0, out  # red: the block refused it before the LFS line
        assert not record.exists()
        (co.main / "GREEN").write_text("", encoding="utf-8")
        commit(co.main, "green", "GREEN")
        code, out = self._push(co.main)
        assert code == 0, out
        assert record.read_text(encoding="utf-8").startswith("refs/heads/main ")

    def test_a_repo_gate_in_githooks_runs_after_the_block(self, co, tmp_path):
        githooks(co.main)
        install(co.main, self._shim(tmp_path, co), sys.executable, ["test"],
                edit_tracked=True, apply=True)
        (co.main / "GREEN").write_text("", encoding="utf-8")
        commit(co.main, "green", "GREEN")
        code, out = self._push(co.main)
        assert code == 0, out
        assert out.index("local-ci: running test") < out.index("[pre-push] repo gate")


@pytest.mark.parametrize("shell", ["sh", "bash", "dash"])
def test_the_hook_and_the_chained_block_parse(shell, tmp_path):
    if shutil.which(shell) is None:
        pytest.skip(f"needs {shell}")
    chained = HUSKY_WRAPPER.replace("\n", "\n" + render_block("Demo", CLI, "python3", ["test"]), 1)
    for body in (render_hook("Demo", CLI, "python3", ["lint", "test"]), chained):
        proc = subprocess.run([shell, "-n"], input=body, text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
