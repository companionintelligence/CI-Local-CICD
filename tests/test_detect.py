"""Stack detection and local check-plan construction."""

from __future__ import annotations

import json

import pytest

from local_ci.detect import detect_node_manager, discover_repos, plan_for


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
