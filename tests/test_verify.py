"""Remote default-branch auditing and drift detection."""

from __future__ import annotations

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
