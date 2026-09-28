"""Stack detection and local check-plan construction."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from local_ci.detect import (
    LOCKFILE_MANAGERS,
    Checkout,
    detect_node_manager,
    install_state,
    discover_repos,
    makefile_targets,
    nested_checkouts,
    plan_for,
    resolve_checkout,
    workspace_globs,
)

from .gitrepo import git, make_checkouts, requires_git


def make_node_repo(tmp_path, scripts, lockfile=None, package_manager=None):
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    pkg = {"name": "repo", "scripts": scripts}
    if package_manager:
        pkg["packageManager"] = package_manager
    (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    if lockfile:
        (root / lockfile).write_text("", encoding="utf-8")
    return root


def _git_init(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _git_commit(root, *files):
    subprocess.run(["git", "add", "--", *files], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "test"], cwd=root, check=True,
                   stdout=subprocess.DEVNULL)


def _install_tree(root, marker, installed_at, inputs_at):
    """A node_modules whose install stamp and manifest mtimes are pinned."""
    path = root / marker
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    os.utime(path, (installed_at, installed_at))
    os.utime(path.parent, (installed_at, installed_at))
    for name in ("package.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock",
                 "package-lock.json"):
        if (root / name).exists():
            os.utime(root / name, (inputs_at, inputs_at))


class TestManagerDetection:
    @pytest.mark.parametrize(
        "lockfile,expected",
        [
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("bun.lockb", "bun"),
            ("package-lock.json", "npm"),
        ],
    )
    def test_lockfile_determines_manager(self, tmp_path, lockfile, expected):
        root = make_node_repo(tmp_path, {}, lockfile=lockfile)
        assert detect_node_manager(root, {}) == expected

    def test_lockfile_beats_a_stale_package_manager_field(self, tmp_path):
        root = make_node_repo(tmp_path, {}, lockfile="yarn.lock")
        assert detect_node_manager(root, {"packageManager": "pnpm@9.0.0"}) == "yarn"

    def test_falls_back_to_declared_field(self, tmp_path):
        root = make_node_repo(tmp_path, {})
        assert detect_node_manager(root, {"packageManager": "pnpm@9.0.0"}) == "pnpm"

    def test_defaults_to_npm(self, tmp_path):
        root = make_node_repo(tmp_path, {})
        assert detect_node_manager(root, {}) == "npm"


class TestNodePlan:
    def test_detects_the_standard_check_set(self, tmp_path):
        root = make_node_repo(
            tmp_path,
            {"lint": "eslint .", "typecheck": "tsc", "test": "vitest run",
             "build": "vite build"},
            lockfile="pnpm-lock.yaml",
        )
        plan = plan_for("repo", root)
        assert plan.stack == "node"
        assert plan.manager == "pnpm"
        assert [c.name for c in plan.checks] == ["lint", "typecheck", "test", "build"]
        assert plan.checks[0].argv == ["pnpm", "run", "lint"]

    def test_npm_uses_the_run_verb(self, tmp_path):
        root = make_node_repo(tmp_path, {"test": "jest"},
                              lockfile="package-lock.json")
        plan = plan_for("repo", root)
        assert plan.checks[0].argv == ["npm", "run", "--silent", "test"]
        assert plan.install.argv == ["npm", "ci"]

    def test_script_aliases_are_resolved(self, tmp_path):
        root = make_node_repo(
            tmp_path, {"type-check": "tsc --noEmit", "test:ci": "vitest run"},
            lockfile="yarn.lock",
        )
        plan = plan_for("repo", root)
        names = {c.name: c.argv[-1] for c in plan.checks}
        assert names["typecheck"] == "type-check"
        assert names["test"] == "test:ci"

    def test_npm_placeholder_test_is_not_run(self, tmp_path):
        root = make_node_repo(
            tmp_path,
            {"test": 'echo "Error: no test specified" && exit 1'},
            lockfile="package-lock.json",
        )
        plan = plan_for("repo", root)
        assert [c.name for c in plan.checks] == []
        assert any("placeholder" in n for n in plan.notes)

    def test_missing_scripts_are_reported_as_notes(self, tmp_path):
        root = make_node_repo(tmp_path, {"test": "vitest run"},
                              lockfile="yarn.lock")
        plan = plan_for("repo", root)
        assert any("no lint script" in n for n in plan.notes)
        assert any("no build script" in n for n in plan.notes)

    def test_missing_node_modules_is_flagged(self, tmp_path):
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="yarn.lock")
        plan = plan_for("repo", root)
        assert any("node_modules missing" in n for n in plan.notes)

    def test_stale_node_modules_is_flagged(self, tmp_path):
        """An install older than the manifest is the eslint-was-just-added case."""
        root = make_node_repo(tmp_path, {"lint": "next lint"},
                              lockfile="pnpm-lock.yaml")
        _install_tree(root, "node_modules/.modules.yaml",
                      installed_at=1_000, inputs_at=2_000)
        plan = plan_for("repo", root)
        assert any("older than package.json" in n for n in plan.notes)

    def test_stale_against_the_lockfile_names_the_lockfile(self, tmp_path):
        root = make_node_repo(tmp_path, {"lint": "next lint"},
                              lockfile="pnpm-lock.yaml")
        _install_tree(root, "node_modules/.modules.yaml",
                      installed_at=1_000, inputs_at=1_000)
        os.utime(root / "pnpm-lock.yaml", (3_000, 3_000))
        plan = plan_for("repo", root)
        assert any("older than pnpm-lock.yaml" in n for n in plan.notes)

    def test_current_node_modules_is_not_flagged(self, tmp_path):
        root = make_node_repo(tmp_path, {"lint": "next lint"},
                              lockfile="pnpm-lock.yaml")
        _install_tree(root, "node_modules/.modules.yaml",
                      installed_at=5_000, inputs_at=1_000)
        plan = plan_for("repo", root)
        assert not any("install required" in n for n in plan.notes)

    def test_unknown_manager_falls_back_to_the_directory_mtime(self, tmp_path):
        """No marker we recognise still beats reporting a stale tree as current."""
        root = make_node_repo(tmp_path, {"lint": "eslint ."},
                              lockfile="pnpm-lock.yaml")
        (root / "node_modules").mkdir()
        os.utime(root / "node_modules", (1_000, 1_000))
        os.utime(root / "pnpm-lock.yaml", (1_000, 1_000))
        os.utime(root / "package.json", (2_000, 2_000))
        assert install_state(root, None) == "node_modules is older than package.json"

    def test_npm_lockfile_stale_is_flagged(self, tmp_path):
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="package-lock.json")
        _install_tree(root, "node_modules/.package-lock.json",
                      installed_at=1_000, inputs_at=1_000)
        os.utime(root / "package-lock.json", (3_000, 3_000))
        plan = plan_for("repo", root)
        assert any("older than package-lock.json" in n for n in plan.notes)

    @pytest.mark.parametrize(
        "manager,lockfile,marker",
        [
            ("pnpm", "pnpm-lock.yaml", "node_modules/.modules.yaml"),
            ("npm", "package-lock.json", "node_modules/.package-lock.json"),
        ],
    )
    def test_every_manager_with_a_marker(self, tmp_path, manager, lockfile, marker):
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile=lockfile)
        _install_tree(root, marker, installed_at=1_000, inputs_at=2_000)
        assert install_state(root, manager) is not None
        _install_tree(root, marker, installed_at=3_000, inputs_at=2_000)
        assert install_state(root, manager) is None

    def test_bun_falls_back_to_the_directory_mtime(self, tmp_path):
        # bun leaves no marker this repo actually writes, so the directory's
        # own mtime is all there is to compare against.
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="bun.lockb")
        (root / "node_modules").mkdir()
        os.utime(root / "node_modules", (1_000, 1_000))
        os.utime(root / "bun.lockb", (2_000, 2_000))
        assert install_state(root, "bun") is not None


class TestUntrackedLockfile:
    """A lockfile that merely exists on disk is not evidence git tracks it.

    Regression for CI-Hermes: a stale, gitignored `package-lock.json` (left over
    from before a `package.json` dependency was added) made `npm ci` fail even
    though the repo has never committed a lockfile at all — its Dockerfile and
    its own GitHub Actions CI both install via plain `npm install`, by design.
    Detection must trust `Path.exists()` only once git confirms the file is
    actually part of the repo, not just sitting in the working tree.
    """

    def test_untracked_lockfile_gets_a_plain_install(self, tmp_path):
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="package-lock.json")
        _git_init(root)
        _git_commit(root, "package.json")  # lockfile deliberately left untracked
        plan = plan_for("repo", root)
        assert plan.manager == "npm"
        assert plan.install.argv == ["npm", "install"]

    def test_tracked_lockfile_still_gets_a_frozen_install(self, tmp_path):
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="package-lock.json")
        _git_init(root)
        _git_commit(root, "package.json", "package-lock.json")
        plan = plan_for("repo", root)
        assert plan.install.argv == ["npm", "ci"]

    def test_untracked_lockfile_does_not_win_manager_detection(self, tmp_path):
        """A leftover lockfile from a different manager must not steer script commands."""
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="yarn.lock")
        _git_init(root)
        _git_commit(root, "package.json")  # yarn.lock deliberately left untracked
        assert detect_node_manager(root, {}) == "npm"

    def test_repo_with_no_lockfile_at_all_gets_a_plain_install(self, tmp_path):
        """A clean checkout of a repo that never committed a lockfile."""
        root = make_node_repo(tmp_path, {"test": "vitest"})
        _git_init(root)
        _git_commit(root, "package.json")
        plan = plan_for("repo", root)
        assert plan.manager == "npm"
        assert plan.install.argv == ["npm", "install"]

    def test_bare_directory_without_git_is_unaffected(self, tmp_path):
        """No repo to check against at all: behaviour matches the old, ungated check."""
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="package-lock.json")
        plan = plan_for("repo", root)
        assert plan.install.argv == ["npm", "ci"]


class TestOtherStacks:
    def test_rust(self, tmp_path):
        root = tmp_path / "rust"
        root.mkdir()
        (root / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
        plan = plan_for("rust", root)
        assert plan.stack == "rust"
        assert [c.name for c in plan.checks] == ["lint", "clippy", "test", "build"]
        assert plan.checks[0].required is False  # fmt is advisory

    def test_flutter(self, tmp_path):
        root = tmp_path / "flutter"
        root.mkdir()
        (root / "pubspec.yaml").write_text("name: app\n", encoding="utf-8")
        plan = plan_for("flutter", root)
        assert plan.stack == "flutter"
        assert plan.install.argv == ["flutter", "pub", "get"]

    def test_go(self, tmp_path):
        root = tmp_path / "go"
        root.mkdir()
        (root / "go.mod").write_text("module x\n", encoding="utf-8")
        assert plan_for("go", root).stack == "go"

    def test_python(self, tmp_path):
        root = tmp_path / "py"
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        assert plan_for("py", root).stack == "python"

    def test_unknown_stack_is_not_runnable(self, tmp_path):
        root = tmp_path / "mystery"
        root.mkdir()
        plan = plan_for("mystery", root)
        assert plan.stack == "unknown"
        assert not plan.runnable

    def test_node_wins_when_several_manifests_exist(self, tmp_path):
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="yarn.lock")
        (root / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
        assert plan_for("repo", root).stack == "node"

    def test_malformed_package_json_does_not_crash(self, tmp_path):
        root = tmp_path / "broken"
        root.mkdir()
        (root / "package.json").write_text("{not json", encoding="utf-8")
        plan = plan_for("broken", root)
        assert plan.stack == "node"
        assert not plan.runnable


class TestDiscovery:
    def test_only_git_repos_are_returned(self, tmp_path):
        (tmp_path / "a" / ".git").mkdir(parents=True)
        (tmp_path / "b").mkdir()
        (tmp_path / "c" / ".git").mkdir(parents=True)
        assert [p.name for p in discover_repos(tmp_path)] == ["a", "c"]

    def test_filter_limits_to_named_repos(self, tmp_path):
        (tmp_path / "a" / ".git").mkdir(parents=True)
        (tmp_path / "c" / ".git").mkdir(parents=True)
        assert [p.name for p in discover_repos(tmp_path, ["c"])] == ["c"]


@requires_git
class TestResolveCheckout:
    """Which tree ``--path`` means, and which policy entry it answers to."""

    def test_main_clone(self, tmp_path):
        co = make_checkouts(tmp_path)
        assert resolve_checkout(co.main) == Checkout("Demo", co.main)

    def test_subdirectory_resolves_to_the_root(self, tmp_path):
        co = make_checkouts(tmp_path)
        (co.main / "src").mkdir()
        assert resolve_checkout(co.main / "src") == Checkout("Demo", co.main)

    def test_worktree_is_its_own_tree_but_named_for_its_main_clone(self, tmp_path):
        # `feature` is the worktree's directory; `[checks.Demo]` is the entry.
        co = make_checkouts(tmp_path)
        assert resolve_checkout(co.worktree) == Checkout("Demo", co.worktree)

    def test_worktree_outside_the_main_clone(self, tmp_path):
        co = make_checkouts(tmp_path)
        elsewhere = tmp_path / "scratch" / "wt"
        git(co.main, "worktree", "add", "-q", "-b", "other", str(elsewhere))
        assert resolve_checkout(elsewhere) == Checkout("Demo", elsewhere.resolve())

    def test_worktree_of_a_bare_repo(self, tmp_path):
        co = make_checkouts(tmp_path)
        bare = tmp_path / "Mirror.git"
        git(tmp_path, "clone", "-q", "--bare", str(co.main), str(bare))
        wt = tmp_path / "mirror-wt"
        git(bare, "worktree", "add", "-q", str(wt), "main")
        assert resolve_checkout(wt).repo == "Mirror"

    def test_not_a_checkout(self, tmp_path):
        with pytest.raises(ValueError, match="not inside a git working tree"):
            resolve_checkout(tmp_path)

    def test_exported_git_dir_is_ignored(self, tmp_path, monkeypatch):
        """The hook inherits ``GIT_DIR`` from the push; ``--path`` must win.

        With it honoured, a subdirectory would be taken for the root and the
        repo named after whichever git dir the variable pointed at.
        """
        co = make_checkouts(tmp_path)
        other = tmp_path / "Other"
        other.mkdir()
        git(other, "init", "-q")
        (co.worktree / "src").mkdir()
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))
        assert resolve_checkout(co.worktree / "src") == Checkout("Demo", co.worktree)


class TestOverrides:
    def test_workdir_redirects_detection(self, tmp_path):
        from local_ci.policy import RepoOverride

        root = tmp_path / "repo"
        (root / "backend").mkdir(parents=True)
        # Decoy root manifest with no test script...
        (root / "package.json").write_text(
            json.dumps({"name": "root", "scripts": {"build": "x"}}), encoding="utf-8"
        )
        # ...and the real project underneath it.
        (root / "backend" / "package.json").write_text(
            json.dumps({"name": "backend", "scripts": {"test": "vitest run"}}),
            encoding="utf-8",
        )
        (root / "backend" / "yarn.lock").write_text("", encoding="utf-8")

        plan = plan_for("repo", root, RepoOverride(workdir="backend"))
        assert plan.path == root / "backend"
        assert [c.name for c in plan.checks] == ["test"]
        assert plan.checks[0].argv == ["yarn", "run", "test"]

    def test_missing_workdir_is_reported_not_guessed(self, tmp_path):
        from local_ci.policy import RepoOverride

        root = tmp_path / "repo"
        root.mkdir()
        plan = plan_for("repo", root, RepoOverride(workdir="nope"))
        assert not plan.runnable
        assert any("workdir `nope` missing" in n for n in plan.notes)

    def test_explicit_commands_replace_detection(self, tmp_path):
        from local_ci.policy import RepoOverride

        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="yarn.lock")
        override = RepoOverride(
            commands=[{"name": "test", "run": ["make", "check"], "required": True}]
        )
        plan = plan_for("repo", root, override)
        assert plan.stack == "custom"
        assert plan.checks[0].argv == ["make", "check"]

    def test_skip_excludes_the_repo(self, tmp_path):
        from local_ci.policy import RepoOverride

        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="yarn.lock")
        plan = plan_for("repo", root, RepoOverride(skip=True))
        assert not plan.runnable
        assert plan.stack == "skipped"


class TestWorkspaceGlobs:
    def test_array_form(self):
        assert workspace_globs({"workspaces": ["packages/*"]}, Path("/x")) == ["packages/*"]

    def test_object_form(self):
        pkg = {"workspaces": {"packages": ["apps/*", "libs/*"], "nohoist": []}}
        assert workspace_globs(pkg, Path("/x")) == ["apps/*", "libs/*"]

    def test_pnpm_workspace_yaml(self, tmp_path):
        (tmp_path / "pnpm-workspace.yaml").write_text(
            "packages:\n  - 'apps/*'\n  - \"libs/*\"\n", encoding="utf-8")
        assert workspace_globs({}, tmp_path) == ["apps/*", "libs/*"]

    def test_pnpm_yaml_stops_at_next_key(self, tmp_path):
        (tmp_path / "pnpm-workspace.yaml").write_text(
            "packages:\n  - 'apps/*'\nonlyBuiltDependencies:\n  - esbuild\n",
            encoding="utf-8")
        assert workspace_globs({}, tmp_path) == ["apps/*"]

    def test_no_workspaces(self, tmp_path):
        assert workspace_globs({}, tmp_path) == []


def _workspace_repo(tmp_path, manager_lockfile, root_scripts, packages):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "root", "scripts": root_scripts,
        "workspaces": ["packages/*"],
    }), encoding="utf-8")
    (tmp_path / manager_lockfile).write_text("", encoding="utf-8")
    for name, scripts in packages.items():
        d = tmp_path / "packages" / name.split("/")[-1]
        d.mkdir(parents=True)
        (d / "package.json").write_text(
            json.dumps({"name": name, "scripts": scripts}), encoding="utf-8")
    return tmp_path


class TestWorkspaceChecks:
    def test_finds_tests_in_packages(self, tmp_path):
        """The CI-Server case: root has lint only, tests live in packages."""
        repo = _workspace_repo(tmp_path, "yarn.lock", {"lint": "eslint"},
                               {"@ci/be-api": {"test": "vitest", "typecheck": "tsc"}})
        plan = plan_for("CI-Server", repo)
        names = [c.name for c in plan.checks]
        assert "lint" in names
        assert "test:@ci/be-api" in names
        assert "typecheck:@ci/be-api" in names

    def test_uses_the_right_workspace_invocation(self, tmp_path):
        repo = _workspace_repo(tmp_path, "yarn.lock", {},
                               {"@ci/be-api": {"test": "vitest"}})
        plan = plan_for("X", repo)
        check = next(c for c in plan.checks if c.name.startswith("test:"))
        assert check.argv == ["yarn", "workspace", "@ci/be-api", "test"]

    def test_pnpm_uses_filter(self, tmp_path):
        repo = _workspace_repo(tmp_path, "pnpm-lock.yaml", {},
                               {"@ci/ui": {"test": "vitest"}})
        plan = plan_for("X", repo)
        check = next(c for c in plan.checks if c.name.startswith("test:"))
        assert check.argv == ["pnpm", "--filter", "@ci/ui", "run", "test"]

    def test_root_test_script_wins_over_packages(self, tmp_path):
        """A root `test` that already fans out must not be duplicated."""
        repo = _workspace_repo(tmp_path, "yarn.lock", {"test": "turbo test"},
                               {"@ci/a": {"test": "vitest"}})
        plan = plan_for("X", repo)
        assert [c.name for c in plan.checks] == ["test"]

    def test_placeholder_test_in_a_package_is_ignored(self, tmp_path):
        repo = _workspace_repo(tmp_path, "yarn.lock", {},
                               {"@ci/a": {"test": 'echo "Error: no test specified"'}})
        plan = plan_for("X", repo)
        assert not any(c.name.startswith("test:") for c in plan.checks)

    def test_package_without_a_name_is_skipped(self, tmp_path):
        repo = _workspace_repo(tmp_path, "yarn.lock", {}, {})
        d = repo / "packages" / "anon"
        d.mkdir(parents=True)
        (d / "package.json").write_text('{"scripts":{"test":"vitest"}}', encoding="utf-8")
        assert plan_for("X", repo).checks == []

    def test_build_is_not_fanned_out(self, tmp_path):
        """Building every package is a release concern, not a check."""
        repo = _workspace_repo(tmp_path, "yarn.lock", {},
                               {"@ci/a": {"build": "tsc"}})
        assert not any(c.name.startswith("build:") for c in plan_for("X", repo).checks)

    def test_note_records_the_source(self, tmp_path):
        repo = _workspace_repo(tmp_path, "yarn.lock", {},
                               {"@ci/a": {"test": "vitest"}})
        plan = plan_for("X", repo)
        assert any("workspace package" in n for n in plan.notes)
        # The misleading "no test script" note must be gone.
        assert "no test script" not in plan.notes


class TestMakefileTargets:
    def test_reads_targets(self, tmp_path):
        (tmp_path / "Makefile").write_text(
            "help: ## desc\nbuild: ## Build images\n\tdocker build .\n"
            "dev:\n\tyarn dev\n", encoding="utf-8")
        assert makefile_targets(tmp_path) == ["help", "build", "dev"]

    def test_ignores_variable_assignment(self, tmp_path):
        (tmp_path / "Makefile").write_text("CC := gcc\nbuild:\n\ttrue\n",
                                           encoding="utf-8")
        assert makefile_targets(tmp_path) == ["build"]

    def test_absent_makefile(self, tmp_path):
        assert makefile_targets(tmp_path) == []

    def test_surfaced_as_a_note(self, tmp_path):
        (tmp_path / "package.json").write_text('{"scripts":{"lint":"eslint"}}',
                                               encoding="utf-8")
        (tmp_path / "Makefile").write_text("build:\n\ttrue\ndev:\n\ttrue\n",
                                           encoding="utf-8")
        plan = plan_for("CI-Server", tmp_path)
        assert any("make build" in n and "make dev" in n for n in plan.notes)


class TestNestedCheckouts:
    """Agent worktrees live inside the repo and poison tree-walking checks.

    Measured on CI-Server: 18 nested checkouts produced 19,096 lint problems
    against a tree that lints clean (eslint exit 0) once they are excluded.
    """

    def _nest(self, tmp_path, parent, names):
        for name in names:
            d = tmp_path / parent / name
            d.mkdir(parents=True)
            (d / ".git").write_text("gitdir: ../..", encoding="utf-8")

    def test_finds_claude_worktrees(self, tmp_path):
        self._nest(tmp_path, ".claude/worktrees", ["agent-a1", "agent-b2"])
        found = nested_checkouts(tmp_path)
        assert found == [".claude/worktrees/agent-a1",
                         ".claude/worktrees/agent-b2"]

    def test_finds_dot_worktrees(self, tmp_path):
        self._nest(tmp_path, ".worktrees", ["video"])
        assert nested_checkouts(tmp_path) == [".worktrees/video"]

    def test_finds_both_roots(self, tmp_path):
        """Excluding only `.worktrees` left 17,978 problems on CI-Server."""
        self._nest(tmp_path, ".worktrees", ["video"])
        self._nest(tmp_path, ".claude/worktrees", ["agent-a1"])
        assert len(nested_checkouts(tmp_path)) == 2

    def test_directory_without_git_is_not_a_checkout(self, tmp_path):
        (tmp_path / ".claude" / "worktrees" / "notes").mkdir(parents=True)
        assert nested_checkouts(tmp_path) == []

    def test_clean_repo_reports_none(self, tmp_path):
        assert nested_checkouts(tmp_path) == []

    def test_limit_caps_the_scan(self, tmp_path):
        self._nest(tmp_path, ".claude/worktrees", [f"a{i}" for i in range(10)])
        assert len(nested_checkouts(tmp_path, limit=3)) == 3

    def test_plan_warns_and_records(self, tmp_path):
        (tmp_path / "package.json").write_text('{"scripts":{"lint":"eslint"}}',
                                               encoding="utf-8")
        self._nest(tmp_path, ".claude/worktrees", ["agent-a1"])
        plan = plan_for("CI-Server", tmp_path)
        assert plan.contaminated == [".claude/worktrees/agent-a1"]
        assert any(n.startswith("WARNING:") for n in plan.notes)

    def test_warning_applies_to_every_stack(self, tmp_path):
        """Rust, Go and Python repos walk trees too."""
        (tmp_path / "Cargo.toml").write_text("[package]", encoding="utf-8")
        self._nest(tmp_path, ".worktrees", ["wt"])
        assert plan_for("X", tmp_path).contaminated

    def test_clean_plan_has_no_warning(self, tmp_path):
        (tmp_path / "package.json").write_text('{"scripts":{"lint":"eslint"}}',
                                               encoding="utf-8")
        plan = plan_for("X", tmp_path)
        assert plan.contaminated == []
        assert not any(n.startswith("WARNING:") for n in plan.notes)
