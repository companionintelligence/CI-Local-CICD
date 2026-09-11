"""Remote default-branch auditing and drift detection."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from local_ci.policy import Policy
from local_ci.verify import (
    RemoteWorkflow,
    RepoAudit,
    find_drift,
    snapshot,
)


def wf(name, gated=False, triggers=(), kept=None):
    return RemoteWorkflow(name=name, gated=gated, triggers=list(triggers), kept=kept)


class TestWorkflowStatus:
    def test_gated_workflow(self):
        assert wf("ci.yml", gated=True, triggers=["workflow_dispatch"]).status == "gated"

    def test_billing_workflow(self):
        w = wf("ci.yml", triggers=["push", "pull_request"])
        assert w.status == "BILLING"
        assert w.billable == ["push", "pull_request"]

    def test_manual_only_is_not_billing(self):
        assert wf("release.yml", triggers=["workflow_dispatch"]).status == "manual"

    def test_workflow_call_alone_is_not_billing(self):
        """A reusable workflow costs nothing until a caller runs it."""
        assert wf("reusable.yml", triggers=["workflow_call"]).status == "manual"

    def test_kept_workflow_is_reported_separately(self):
        w = wf("deploy.yml", triggers=["push"], kept="*/deploy.yml")
        assert w.status == "kept"

    def test_gated_beats_kept(self):
        w = wf("deploy.yml", gated=True, triggers=["workflow_dispatch"],
               kept="*/deploy.yml")
        assert w.status == "gated"


class TestBillingSurvivesGating:
    """The regression this module exists to prevent.

    `status` returning "gated" used to be the end of the story: `billing` was
    derived from `status`, so a gated workflow recorded `billing: []` and every
    consumer read that as "this repo costs nothing". It actually meant "we
    switched it off before anyone priced it", and a restore decision was made
    on the difference.
    """

    def _gated(self, source):
        from local_ci.workflows import gate

        text, changed = gate(source)
        assert changed, "fixture must actually be gated for this test to mean anything"
        return text

    def test_gated_workflow_still_reports_it_would_bill(self):
        from local_ci.workflows import restored_triggers, runner_labels

        text = self._gated(
            "name: CI\non:\n  push:\n    branches: [main]\n  pull_request:\n"
            "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        )
        w = RemoteWorkflow(
            name="ci.yml", gated=True, triggers=["workflow_dispatch"],
            restored=restored_triggers(text), runners=runner_labels(text),
        )
        # Current state: switched off, and honestly reported as such.
        assert w.status == "gated"
        assert w.billable == []
        # Billing classification survives anyway.
        assert w.restored_billable == ["push", "pull_request"]
        assert w.would_bill is True
        assert w.verdict.multiplier == 1

    def test_gated_macos_workflow_keeps_its_10x_rate(self):
        from local_ci.workflows import restored_triggers, runner_labels

        text = self._gated(
            "name: Build\non:\n  schedule:\n    - cron: '0 3 * * *'\n"
            "jobs:\n  build:\n    runs-on: macos-latest\n"
        )
        w = RemoteWorkflow(
            name="build.yml", gated=True, triggers=["workflow_dispatch"],
            restored=restored_triggers(text), runners=runner_labels(text),
        )
        assert w.status == "gated"
        assert w.would_bill is True
        assert w.verdict.multiplier == 10

    def test_gated_manual_only_workflow_is_genuinely_free(self):
        """The fix must not flip every gated workflow to 'billable'."""
        w = RemoteWorkflow(
            name="release.yml", gated=True, triggers=["workflow_dispatch"],
            restored=["workflow_dispatch"], runners=["ubuntu-latest"],
        )
        assert w.would_bill is False

    def test_gated_self_hosted_workflow_is_free(self):
        w = RemoteWorkflow(
            name="ci.yml", gated=True, triggers=["workflow_dispatch"],
            restored=["push"], runners=["self-hosted"],
        )
        assert w.would_bill is False
        assert w.verdict.reason == "self-hosted runners only"

    def test_verdict_ignores_the_gated_flag_entirely(self):
        """Same inputs, both gate states, identical verdict."""
        kwargs = dict(name="ci.yml", triggers=["push"], restored=["push"],
                      runners=["ubuntu-latest"])
        on = RemoteWorkflow(gated=False, **kwargs)
        off = RemoteWorkflow(gated=True, **kwargs)
        assert on.would_bill == off.would_bill is True
        assert on.verdict.multiplier == off.verdict.multiplier

    def test_restored_defaults_to_live_triggers(self):
        assert RemoteWorkflow(name="a.yml", gated=False,
                              triggers=["push"]).restored == ["push"]

    def test_end_to_end_through_audit_worktree(self, tmp_path):
        """A gated repo on disk reports zero billing but a real restore cost."""
        from local_ci.verify import audit_worktree
        from local_ci.workflows import gate

        d = tmp_path / ".github" / "workflows"
        d.mkdir(parents=True)
        gated, _ = gate(
            "name: CI\non:\n  push:\n  pull_request:\n"
            "jobs:\n  t:\n    runs-on: ubuntu-latest\n"
        )
        (d / "ci.yml").write_text(gated, encoding="utf-8")

        audit = audit_worktree(tmp_path, "CI-Hub", Policy(), "dev")
        assert audit.billing == []                       # nothing costs money now
        assert [w.name for w in audit.billable_if_restored] == ["ci.yml"]

    def test_snapshot_records_billing_and_restore_cost_separately(self, tmp_path):
        from local_ci.verify import audit_worktree
        from local_ci.workflows import gate

        d = tmp_path / ".github" / "workflows"
        d.mkdir(parents=True)
        gated, _ = gate("name: CI\non:\n  push:\njobs:\n  t:\n    "
                        "runs-on: ubuntu-latest\n")
        (d / "ci.yml").write_text(gated, encoding="utf-8")

        snap = snapshot([audit_worktree(tmp_path, "R", Policy(), "main")])
        assert snap["R"]["billing"] == []
        assert snap["R"]["billable_if_restored"] == ["ci.yml"]

    def test_drift_still_keys_off_live_billing(self):
        """The new field must not make every gated repo look like drift."""
        base = {"A": {"base": "main", "billing": [],
                      "billable_if_restored": ["ci.yml"], "known": ["ci.yml"]}}
        assert find_drift(base, base) == []


class TestRepoAudit:
    def test_counts_group_by_status(self):
        audit = RepoAudit("CI-Hub", "dev", workflows=[
            wf("a.yml", gated=True, triggers=["workflow_dispatch"]),
            wf("b.yml", gated=True, triggers=["workflow_dispatch"]),
            wf("c.yml", triggers=["push"]),
        ])
        assert audit.counts == {"gated": 2, "BILLING": 1}
        assert [w.name for w in audit.billing] == ["c.yml"]

    def test_snapshot_skips_errored_repos(self):
        audits = [
            RepoAudit("Good", "main", workflows=[wf("a.yml", triggers=["push"])]),
            RepoAudit("Bad", error="cannot resolve default branch"),
        ]
        snap = snapshot(audits)
        assert set(snap) == {"Good"}
        assert snap["Good"]["billing"] == ["a.yml"]


class TestDrift:
    def test_no_drift_when_unchanged(self):
        state = {"A": {"base": "main", "billing": ["x.yml"], "known": ["x.yml"]}}
        assert find_drift(state, state) == []

    def test_no_drift_when_fully_clean(self):
        base = {"A": {"base": "main", "billing": [], "known": ["x.yml"]}}
        assert find_drift(base, base) == []

    def test_regression_is_drift(self):
        base = {"A": {"base": "main", "billing": [], "known": ["ci.yml"]}}
        now = {"A": {"base": "main", "billing": ["ci.yml"], "known": ["ci.yml"]}}
        (drift,) = find_drift(base, now)
        assert drift.kind == "regressed"
        assert "regained automatic triggers" in drift.describe()

    def test_new_ungated_workflow_is_drift(self):
        """The CI-Marketplace build-llmfit case: added after the gating pass."""
        base = {"M": {"base": "dev", "billing": [], "known": ["ci.yml"]}}
        now = {"M": {"base": "dev", "billing": ["build-llmfit.yml"],
                     "known": ["ci.yml", "build-llmfit.yml"]}}
        (drift,) = find_drift(base, now)
        assert drift.kind == "new-workflow"
        assert drift.workflows == ["build-llmfit.yml"]

    def test_new_repo_is_drift(self):
        now = {"New": {"base": "main", "billing": ["ci.yml"], "known": ["ci.yml"]}}
        (drift,) = find_drift({}, now)
        assert drift.kind == "new-repo"

    def test_improvement_is_never_drift(self):
        """Gating more than the baseline must not page anyone."""
        base = {"A": {"base": "main", "billing": ["a.yml", "b.yml"],
                      "known": ["a.yml", "b.yml"]}}
        now = {"A": {"base": "main", "billing": ["a.yml"],
                     "known": ["a.yml", "b.yml"]}}
        assert find_drift(base, now) == []

    def test_baseline_billing_is_tolerated(self):
        """A keep-listed deploy workflow that always billed is not drift."""
        base = {"A": {"base": "dev", "billing": ["deploy.yml"],
                      "known": ["deploy.yml"]}}
        now = dict(base)
        assert find_drift(base, now) == []

    def test_regression_and_addition_reported_separately(self):
        base = {"A": {"base": "main", "billing": [], "known": ["old.yml"]}}
        now = {"A": {"base": "main", "billing": ["old.yml", "new.yml"],
                     "known": ["old.yml", "new.yml"]}}
        kinds = {d.kind: d.workflows for d in find_drift(base, now)}
        assert kinds == {"regressed": ["old.yml"], "new-workflow": ["new.yml"]}

    def test_repo_disappearing_is_not_drift(self):
        base = {"A": {"base": "main", "billing": ["x.yml"], "known": ["x.yml"]}}
        assert find_drift(base, {}) == []


class TestAuditWorktree:
    """`ship` audits the checkout it is about to commit, not the API."""

    def _worktree(self, tmp_path, files):
        d = tmp_path / ".github" / "workflows"
        d.mkdir(parents=True)
        for name, text in files.items():
            (d / name).write_text(text, encoding="utf-8")
        return tmp_path

    def test_reads_workflows_from_disk(self, tmp_path):
        from local_ci.verify import audit_worktree

        wt = self._worktree(tmp_path, {
            "ci.yml": "name: CI\non:\n  push:\n    branches: [main]\njobs: {}\n",
            "notes.md": "ignored",
        })
        audit = audit_worktree(wt, "CI-Hub", Policy(), "dev")
        assert audit.base == "dev"
        assert [w.name for w in audit.workflows] == ["ci.yml"]
        assert audit.workflows[0].title == "CI"
        assert audit.workflows[0].status == "BILLING"

    def test_picks_up_dispatch_inputs(self, tmp_path):
        from local_ci.verify import audit_worktree

        wt = self._worktree(tmp_path, {"release.yml": (
            "name: Release\non:\n  workflow_dispatch:\n    inputs:\n"
            "      tag:\n        required: true\n        type: string\njobs: {}\n"
        )})
        audit = audit_worktree(wt, "X", Policy(), "main")
        assert [i.name for i in audit.workflows[0].inputs] == ["tag"]
        assert audit.workflows[0].inputs[0].required

    def test_missing_workflows_dir(self, tmp_path):
        from local_ci.verify import audit_worktree

        assert audit_worktree(tmp_path, "X", Policy(), "main").workflows == []


# `on:` and `runs-on:` copied verbatim from the real file on
# companionintelligence/CI-Portal@dev. The steps are elided; nothing below the
# runner label affects the classification.
CI_PORTAL_DEPLOY = """\
name: Deploy CI-Cloud

on:
  push:
    branches:
      - staging
      - main
      - dev
  workflow_dispatch:
    inputs:
      environment:
        description: 'Environment to deploy to'
        required: true
        type: choice
        options:
          - dev
          - staging
          - production

jobs:
  deploy:
    name: Deploy
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
"""


class TestKeptWorkflowsAreNotDormant:
    """Regression for the defect merged in #9.

    ``cmd_verify`` built its "dormant, only gated" list as
    ``[w for w in a.billable_if_restored if w.status != "BILLING"]``.
    ``status`` returns ``"kept"`` for a policy-protected workflow, so
    CI-Portal's ``deploy.yml`` - ungated, on ``push`` to main, hosted Linux,
    private repo - was printed as dormant while it was deploying to production
    (run at 2026-08-10T23:27:40Z, conclusion success).
    """

    def _audit(self, tmp_path, keep=("CI-Portal/deploy.yml",)):
        from local_ci.verify import audit_worktree

        d = tmp_path / ".github" / "workflows"
        d.mkdir(parents=True)
        (d / "deploy.yml").write_text(CI_PORTAL_DEPLOY, encoding="utf-8")
        return audit_worktree(tmp_path, "CI-Portal", Policy(keep=list(keep)),
                              "dev", private=True)

    def test_the_facts_the_classification_rests_on(self, tmp_path):
        w = self._audit(tmp_path).workflows[0]
        assert w.gated is False
        assert w.billable == ["push"]
        assert w.kept == "CI-Portal/deploy.yml"
        assert w.targets == [["ubuntu-latest"]]
        assert w.would_bill is True

    def test_it_is_live_billing_not_dormant(self, tmp_path):
        audit = self._audit(tmp_path)
        assert [w.name for w in audit.live_billing] == ["deploy.yml"]
        assert audit.restore_cost == [], "it is running; it cannot be dormant"

    def test_the_old_expression_is_the_one_that_was_wrong(self, tmp_path):
        """Pin the exact defect so it cannot come back by copy-paste."""
        audit = self._audit(tmp_path)
        old = [w for w in audit.billable_if_restored if w.status != "BILLING"]
        assert [w.name for w in old] == ["deploy.yml"]      # what #9 printed
        assert audit.restore_cost == []                     # what is true

    def test_restore_cost_only_ever_contains_gated_workflows(self, tmp_path):
        from local_ci.verify import audit_worktree
        from local_ci.workflows import gate

        d = tmp_path / ".github" / "workflows"
        d.mkdir(parents=True)
        (d / "deploy.yml").write_text(CI_PORTAL_DEPLOY, encoding="utf-8")
        gated, changed = gate(CI_PORTAL_DEPLOY)
        assert changed
        (d / "ci.yml").write_text(gated, encoding="utf-8")

        audit = audit_worktree(tmp_path, "CI-Portal",
                               Policy(keep=["CI-Portal/deploy.yml"]), "dev",
                               private=True)
        assert [w.name for w in audit.live_billing] == ["deploy.yml"]
        assert [w.name for w in audit.restore_cost] == ["ci.yml"]
        assert all(w.gated for w in audit.restore_cost)

    def test_billing_stays_the_policy_violation_set(self, tmp_path):
        """`billing` still excludes kept workflows - drift baselines depend on it."""
        assert self._audit(tmp_path).billing == []

    def test_without_the_keep_pattern_it_is_a_policy_violation_too(self, tmp_path):
        audit = self._audit(tmp_path, keep=())
        assert [w.name for w in audit.billing] == ["deploy.yml"]
        assert [w.name for w in audit.live_billing] == ["deploy.yml"]

    def test_snapshot_records_all_three_sets(self, tmp_path):
        snap = snapshot([self._audit(tmp_path)])["CI-Portal"]
        assert snap["billing"] == []
        assert snap["live_billing"] == ["deploy.yml"]
        assert snap["restore_cost"] == []
        assert snap["billable_if_restored"] == ["deploy.yml"]

    def test_weighted_by_runner_os(self, tmp_path):
        assert self._audit(tmp_path).linux_equivalent == 1


class TestActivityOnRemoteWorkflow:
    def test_activity_defaults_to_unmeasured(self):
        from local_ci.activity import UNKNOWN

        w = wf("deploy.yml", triggers=["push"], kept="*/deploy.yml")
        assert w.activity is None
        assert w.spend.activity == UNKNOWN
        assert w.is_billing_now is True, "unmeasured must not read as free"

    def test_recent_runs_are_reported_without_changing_the_state(self):
        from local_ci.activity import ACTIVE, from_runs
        from local_ci.spend import LIVE_BILLING

        w = wf("deploy.yml", triggers=["push"], kept="*/deploy.yml")
        w.runners = ["ubuntu-latest"]
        w.targets = [["ubuntu-latest"]]
        w.activity = from_runs([{"created_at": "2026-08-10T23:27:40Z",
                                 "conclusion": "success"}],
                               now=datetime(2026, 8, 10, 23, 59,
                                            tzinfo=timezone.utc))
        assert w.spend.activity == ACTIVE
        assert w.spend.state == LIVE_BILLING

    def test_measure_activity_only_touches_billable_states(self):
        from local_ci.verify import measure_activity

        billing = wf("deploy.yml", triggers=["push"], kept="*/deploy.yml")
        manual = wf("release.yml", triggers=["workflow_dispatch"])
        audit = RepoAudit("CI-Portal", "dev",
                          workflows=[billing, manual], private=True)

        asked = []

        def fake(slug, name, window_days=30):
            from local_ci.activity import RunActivity
            asked.append(name)
            return RunActivity(known=True)

        measure_activity([audit], {"CI-Portal": "CI-Portal"}, fetch=fake)
        assert asked == ["deploy.yml"]
        assert manual.activity is None

    def test_measure_activity_skips_errored_repos(self):
        from local_ci.verify import measure_activity

        audit = RepoAudit("Bad", error="cannot resolve default branch")
        measure_activity([audit], {"Bad": "Bad"},
                         fetch=lambda *a, **k: pytest.fail("must not fetch"))
