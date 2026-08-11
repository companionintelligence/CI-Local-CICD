"""Run history - the "has it actually run" axis."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from local_ci.activity import (
    ACTIVE,
    DEFAULT_WINDOW_DAYS,
    DORMANT,
    UNKNOWN,
    RunActivity,
    fetch_activity,
    from_runs,
    parse_timestamp,
)

NOW = datetime(2026, 8, 10, 23, 59, 0, tzinfo=timezone.utc)


def run(days_ago, conclusion="success"):
    stamp = NOW - timedelta(days=days_ago)
    return {"created_at": stamp.isoformat().replace("+00:00", "Z"),
            "conclusion": conclusion}


class TestParseTimestamp:
    def test_github_z_suffix(self):
        assert parse_timestamp("2026-08-10T23:27:40Z") == datetime(
            2026, 8, 10, 23, 27, 40, tzinfo=timezone.utc
        )

    def test_garbage_is_none_not_an_exception(self):
        assert parse_timestamp("not a date") is None
        assert parse_timestamp("") is None

    def test_naive_timestamp_is_assumed_utc(self):
        assert parse_timestamp("2026-08-10T23:27:40").tzinfo is timezone.utc


class TestFromRuns:
    def test_recent_run_is_active(self):
        a = from_runs([run(1)], now=NOW)
        assert a.state == ACTIVE
        assert a.ran_recently and a.succeeded_recently

    def test_old_runs_only_is_dormant(self):
        a = from_runs([run(200), run(90)], now=NOW)
        assert a.state == DORMANT
        assert not a.ran_recently
        assert a.last_run_at == NOW - timedelta(days=90)

    def test_empty_history_is_known_and_dormant(self):
        """GitHub answering "no runs" is data, not absence of data."""
        a = from_runs([], now=NOW)
        assert a.known is True
        assert a.state == DORMANT

    def test_failures_count_as_activity_but_not_as_success(self):
        a = from_runs([run(2, "failure")], now=NOW)
        assert a.ran_recently is True
        assert a.succeeded_recently is False
        assert a.last_success_at is None

    def test_counts_only_runs_inside_the_window(self):
        a = from_runs([run(1), run(5), run(400)], now=NOW)
        assert a.runs_in_window == 2
        assert a.last_run_at == NOW - timedelta(days=1)

    def test_window_is_configurable(self):
        assert from_runs([run(20)], now=NOW, window_days=7).state == DORMANT
        assert from_runs([run(20)], now=NOW, window_days=60).state == ACTIVE

    def test_unparseable_entries_are_skipped_not_fatal(self):
        a = from_runs([{"created_at": "junk"}, run(1)], now=NOW)
        assert a.runs_in_window == 1

    def test_boundary_run_is_inside_the_window(self):
        a = from_runs([run(DEFAULT_WINDOW_DAYS)], now=NOW)
        assert a.state == ACTIVE


class TestUnknownIsNotDormant:
    """The distinction the whole module exists for."""

    def test_unknown_state(self):
        a = RunActivity.unknown()
        assert a.state == UNKNOWN
        assert a.ran_recently is False
        assert a.succeeded_recently is False

    def test_unknown_describes_itself_honestly(self):
        assert "not checked" in RunActivity.unknown().describe()

    def test_dormant_describes_itself_differently(self):
        assert "no runs" in from_runs([], now=NOW).describe()


class TestFetchActivity:
    def test_parses_the_api_payload(self):
        calls = []

        def fake(args):
            calls.append(args)
            return {"workflow_runs": [run(0)]}

        a = fetch_activity("o/r", "deploy.yml", now=NOW, runner=fake)
        assert a.state == ACTIVE
        assert "repos/o/r/actions/workflows/deploy.yml/runs" in calls[0][1]

    def test_failed_lookup_is_unknown_not_dormant(self):
        a = fetch_activity("o/r", "deploy.yml", runner=lambda args: None)
        assert a.state == UNKNOWN

    def test_malformed_payload_is_unknown(self):
        a = fetch_activity("o/r", "x.yml", runner=lambda args: {"nope": 1})
        assert a.state == UNKNOWN

    def test_not_branch_scoped_by_default(self):
        """A tag-triggered release bills too; filtering by branch hides it."""
        seen = []
        fetch_activity("o/r", "release.yml",
                       runner=lambda args: seen.append(args) or {"workflow_runs": []})
        assert "branch=" not in seen[0][1]

    def test_branch_scope_is_available_when_asked(self):
        seen = []
        fetch_activity("o/r", "ci.yml", branch="dev",
                       runner=lambda args: seen.append(args) or {"workflow_runs": []})
        assert "branch=dev" in seen[0][1]
