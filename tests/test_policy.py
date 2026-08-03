"""Which workflows get gated, and why."""

from __future__ import annotations

import pytest

from local_ci.policy import Policy, classify, looks_like_ship_path

from .test_workflows import MANUAL_ONLY, MAPPING, REUSABLE

POLICY_TOML = """\
[workflows]
keep = ["CI-Portal/deploy.yml", "Companion-Pipe/release-*.yml"]
force_gate = ["CI-Hub/nightly-release.yml"]
"""


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / "ci-local.toml"
    path.write_text(POLICY_TOML, encoding="utf-8")
    return Policy.load(path)


class TestPolicyLoading:
    def test_missing_file_yields_empty_policy(self, tmp_path):
        loaded = Policy.load(tmp_path / "absent.toml")
        assert loaded.keep == []
        assert loaded.force_gate == []
        assert loaded.source is None

    def test_patterns_are_globs(self, policy):
        assert policy.matches_keep("Companion-Pipe/release-cli.yml")
        assert policy.matches_keep("Companion-Pipe/release-app.yml")
        assert policy.matches_keep("Companion-Pipe/ci.yml") is None


class TestClassify:
    def test_billable_triggers_are_gated_by_default(self, policy):
        decision = classify("CI-Server", "ci.yml", MAPPING, policy)
        assert decision.action == "gate"
        assert decision.will_change
        assert decision.reason == "billable triggers"

    def test_keep_list_wins_over_the_default(self, policy):
        decision = classify("CI-Portal", "deploy.yml", MAPPING, policy)
        assert decision.action == "keep"
        assert not decision.will_change
        assert "CI-Portal/deploy.yml" in decision.reason

    def test_reusable_workflows_are_gated_but_stay_callable(self, policy):
        # workflow_call is not a reason to keep the automatic triggers: gating
        # strips `push` and leaves `workflow_call`, so callers still work.
        decision = classify("CI-Hub", "shared.yml", REUSABLE, policy)
        assert decision.action == "gate"

        from local_ci.workflows import declared_triggers, gate

        gated, changed = gate(REUSABLE)
        assert changed
        assert declared_triggers(gated) == ["workflow_dispatch", "workflow_call"]

    def test_workflow_call_only_needs_no_change(self, policy):
        text = REUSABLE.replace("  push:\n    branches: [main]\n", "")
        decision = classify("CI-Hub", "shared.yml", text, policy)
        assert decision.action == "already-manual"

    def test_force_gate_overrides_everything(self, policy):
        decision = classify("CI-Hub", "nightly-release.yml", MAPPING, policy)
        assert decision.action == "gate"
        assert "force_gate" in decision.reason

    def test_manual_only_workflow_needs_no_change(self, policy):
        decision = classify("CI-Hub", "desktop-release.yml", MANUAL_ONLY, policy)
        assert decision.action == "already-manual"
        assert not decision.will_change

    def test_already_gated_workflow_is_recognised(self, policy):
        from local_ci.workflows import gate

        gated, _ = gate(MAPPING)
        decision = classify("CI-Server", "ci.yml", gated, policy)
        assert decision.action == "already-gated"
        assert not decision.will_change

    def test_ident_is_repo_slash_workflow(self, policy):
        decision = classify("CI-Server", "ci.yml", MAPPING, policy)
        assert decision.ident == "CI-Server/ci.yml"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("deploy.yml", True),
        ("desktop-release.yml", True),
        ("docker-publish.yml", True),
        ("pages-deploy.yml", True),
        ("ci.yml", False),
        ("e2e.yml", False),
        ("codeql.yml", False),
    ],
)
def test_ship_path_heuristic(name, expected):
    assert looks_like_ship_path(name) is expected


class TestRepoAliases:
    def test_alias_maps_clone_dir_to_github_name(self, tmp_path):
        path = tmp_path / "p.toml"
        path.write_text(
            '[workflows]\nkeep = []\n\n[repos]\n'
            'aliases = { CI-Applet = "CI-Capture" }\n',
            encoding="utf-8",
        )
        loaded = Policy.load(path)
        assert loaded.github_name("CI-Applet") == "CI-Capture"

    def test_unaliased_repo_passes_through(self, policy):
        assert policy.github_name("CI-Server") == "CI-Server"


class TestExclusions:
    def test_excluded_repo_is_reported(self, tmp_path):
        path = tmp_path / "p.toml"
        path.write_text(
            '[workflows]\nkeep = []\n\n[repos]\nexclude = ["CI-Gateway"]\n',
            encoding="utf-8",
        )
        loaded = Policy.load(path)
        assert loaded.is_excluded("CI-Gateway")
        assert not loaded.is_excluded("CI-Hub")

    def test_exclusions_are_globs(self, tmp_path):
        path = tmp_path / "p.toml"
        path.write_text(
            '[workflows]\nkeep = []\n\n[repos]\nexclude = ["ZARCHIVE-*"]\n',
            encoding="utf-8",
        )
        loaded = Policy.load(path)
        assert loaded.is_excluded("ZARCHIVE-CI-Connect")
        assert not loaded.is_excluded("CI-Connect")

    def test_no_exclusions_by_default(self, policy):
        assert not policy.is_excluded("anything")
