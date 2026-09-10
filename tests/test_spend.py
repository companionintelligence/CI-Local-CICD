"""The three-axis spend state machine.

The defect these cover: PR #9's report asked ``status != "BILLING"`` and called
the remainder "dormant, only not billing because it is gated". ``status``
returns ``"kept"`` for a policy-protected workflow, so every kept workflow -
including CI-Portal's live production ``deploy.yml`` - landed in the dormant
column while it was deploying to production.

So the matrix below walks all three axes independently and asserts that each
one is decided on its own evidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from local_ci.activity import ACTIVE, DORMANT, UNKNOWN, RunActivity, from_runs
from local_ci.billing import classify_billing
from local_ci.spend import (
    GATED_BILLABLE,
    GATED_FREE,
    LIVE_BILLING,
    LIVE_FREE,
    MANUAL,
    classify_spend,
    linux_equivalent,
    state_counts,
)

NOW = datetime(2026, 8, 10, 23, 59, 0, tzinfo=timezone.utc)
BILLABLE = classify_billing(["push"], ["ubuntu-latest"], private=True)
FREE = classify_billing(["push"], ["self-hosted"], private=True)
NO_TRIGGERS = classify_billing(["workflow_dispatch"], ["ubuntu-latest"])


def active():
    return from_runs([{"created_at": NOW.isoformat().replace("+00:00", "Z"),
                       "conclusion": "success"}], now=NOW)


def dormant():
    return from_runs([], now=NOW)


class TestTheFullMatrix:
    """gated/kept x billable/free x active/dormant, all eight live cases."""

    @pytest.mark.parametrize("gated,triggers_live,verdict,expected", [
        # Nothing gated it and something automatic is live.
        (False, True, BILLABLE, LIVE_BILLING),
        (False, True, FREE, LIVE_FREE),
        # Gated: switched off, so nothing bills today either way.
        (True, False, BILLABLE, GATED_BILLABLE),
        (True, False, FREE, GATED_FREE),
        # Never gated, but only a human can start it.
        (False, False, NO_TRIGGERS, MANUAL),
    ])
    def test_gating_axis_times_cost_axis(self, gated, triggers_live, verdict,
                                         expected):
        assert classify_spend(triggers_live, verdict, gated=gated).state == expected

    @pytest.mark.parametrize("activity,expected", [
        (None, UNKNOWN),
        (active(), ACTIVE),
        (dormant(), DORMANT),
    ])
    def test_activity_axis_is_reported_but_never_renames_the_state(
        self, activity, expected
    ):
        s = classify_spend(True, BILLABLE, activity=activity)
        assert s.activity == expected
        assert s.state == LIVE_BILLING, "activity must not change the state"


class TestKeptIsNotAnAxis:
    """The regression. `kept` is a decision we made, not a property of cost."""

    def test_kept_and_billable_and_running_is_live_billing(self):
        """CI-Portal/deploy.yml: kept, ungated, push, hosted linux, private."""
        s = classify_spend(True, BILLABLE, gated=False, activity=active(),
                           kept="CI-Portal/deploy.yml")
        assert s.state == LIVE_BILLING
        assert s.is_billing_now is True
        assert s.is_restore_cost is False, "it is running; nothing to restore"

    def test_kept_does_not_change_the_state(self):
        without = classify_spend(True, BILLABLE)
        with_keep = classify_spend(True, BILLABLE, kept="*/deploy.yml")
        assert without.state == with_keep.state

    def test_kept_is_still_carried_for_reporting(self):
        s = classify_spend(True, BILLABLE, kept="*/deploy.yml")
        assert s.kept == "*/deploy.yml"
        assert "kept by `*/deploy.yml`" in s.describe()

    def test_kept_never_lands_in_the_restore_cost_bucket(self):
        for activity in (None, active(), dormant()):
            s = classify_spend(True, BILLABLE, gated=False, activity=activity,
                               kept="*/deploy.yml")
            assert s.is_restore_cost is False


class TestBillingNowVersusRestoreCost:
    def test_the_two_buckets_are_disjoint(self):
        live = classify_spend(True, BILLABLE)
        off = classify_spend(False, BILLABLE, gated=True)
        assert live.is_billing_now and not live.is_restore_cost
        assert off.is_restore_cost and not off.is_billing_now

    def test_free_workflows_are_in_neither(self):
        for s in (classify_spend(True, FREE),
                  classify_spend(False, FREE, gated=True),
                  classify_spend(False, NO_TRIGGERS)):
            assert not s.is_billing_now
            assert not s.is_restore_cost

    def test_dormant_live_billing_still_bills(self):
        """No runs for a month does not make a live trigger free."""
        s = classify_spend(True, BILLABLE, activity=dormant())
        assert s.state == LIVE_BILLING
        assert s.is_billing_now is True
        assert s.activity == DORMANT


class TestLiveTriggersBeatTheGatingMarker:
    def test_marker_says_gated_but_triggers_are_live(self):
        """A hand-edited file bills on what GitHub evaluates, not the comment."""
        s = classify_spend(True, BILLABLE, gated=True)
        assert s.state == LIVE_BILLING
        assert s.gated is True   # recorded, so the contradiction is visible


class TestWeighting:
    def test_macos_counts_as_ten_linux(self):
        mac = classify_spend(True, classify_billing(["push"], ["macos-latest"]))
        linux = classify_spend(True, BILLABLE)
        assert linux_equivalent([mac, linux]) == 11

    def test_state_counts(self):
        states = [classify_spend(True, BILLABLE),
                  classify_spend(True, BILLABLE, kept="k"),
                  classify_spend(False, BILLABLE, gated=True)]
        assert state_counts(states) == {LIVE_BILLING: 2, GATED_BILLABLE: 1}
