"""Stack detection and local check-plan construction."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from local_ci.detect import (
    LOCKFILE_MANAGERS,
    NODE_INSTALL_MARKERS,
    detect_node_manager,
    discover_repos,
    makefile_targets,
    nested_checkouts,
    node_install_reason,
    plan_for,
    workspace_globs,
)


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


def install_tree(root, manager, installed_at, lock_at, marker=True):
    """A `node_modules` installed at ``installed_at``, lockfiles at ``lock_at``.

    Timestamps are set explicitly rather than slept into place: the whole point
    of the check is the ordering between them, and a test that waits for the
    clock is a test that flakes on a fast disk.
    """
    modules = root / "node_modules"
    modules.mkdir(exist_ok=True)
    if marker:
        name = NODE_INSTALL_MARKERS.get(manager)
        if name:
            (modules / name).write_text("{}", encoding="utf-8")
            os.utime(modules / name, (installed_at, installed_at))
    os.utime(modules, (installed_at, installed_at))
    for lockfile, mgr in LOCKFILE_MANAGERS:
        if mgr == manager and (root / lockfile).exists():
            os.utime(root / lockfile, (lock_at, lock_at))
    return root


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
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="package-lock.json")
        install_tree(root, "npm", installed_at=1000, lock_at=2000)
        plan = plan_for("repo", root)
        assert any("package-lock.json is newer" in n for n in plan.notes)


class TestNodeInstallReason:
    """`node_modules` existing is not `node_modules` being current."""

    def test_absent_tree(self, tmp_path):
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="package-lock.json")
        assert node_install_reason(root, "npm") == "node_modules missing"

    def test_tree_newer_than_lockfile_is_current(self, tmp_path):
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="package-lock.json")
        install_tree(root, "npm", installed_at=2000, lock_at=1000)
        assert node_install_reason(root, "npm") is None

    def test_lockfile_newer_than_tree_is_stale(self, tmp_path):
        # The real case: a dependency bump lands via git, the hoisted top-level
        # copy stays whatever was installed before it, and the checks quietly
        # run against a version the lockfile does not describe.
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="package-lock.json")
        install_tree(root, "npm", installed_at=1000, lock_at=2000)
        assert node_install_reason(root, "npm") == (
            "package-lock.json is newer than the installed tree"
        )

    @pytest.mark.parametrize(
        "manager,lockfile",
        [("pnpm", "pnpm-lock.yaml"), ("yarn", "yarn.lock"), ("bun", "bun.lockb")],
    )
    def test_every_manager(self, tmp_path, manager, lockfile):
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile=lockfile)
        install_tree(root, manager, installed_at=1000, lock_at=2000)
        assert node_install_reason(root, manager) == (
            f"{lockfile} is newer than the installed tree"
        )
        install_tree(root, manager, installed_at=3000, lock_at=2000)
        assert node_install_reason(root, manager) is None

    def test_bun_falls_back_to_the_directory_mtime(self, tmp_path):
        # bun leaves no marker file, so the directory's own mtime is all there is.
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="bun.lockb")
        install_tree(root, "bun", installed_at=1000, lock_at=2000, marker=False)
        assert not (root / "node_modules" / ".package-lock.json").exists()
        assert node_install_reason(root, "bun") is not None

    def test_missing_marker_falls_back_to_the_directory_mtime(self, tmp_path):
        # npm 6 and hand-made trees have no hidden lockfile to read.
        root = make_node_repo(tmp_path, {"test": "vitest"}, lockfile="package-lock.json")
        install_tree(root, "npm", installed_at=1000, lock_at=2000, marker=False)
        assert node_install_reason(root, "npm") is not None

    def test_no_lockfile_means_nothing_to_be_stale_against(self, tmp_path):
        root = make_node_repo(tmp_path, {"test": "vitest"})
        install_tree(root, "npm", installed_at=1000, lock_at=2000)
        assert node_install_reason(root, "npm") is None


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
