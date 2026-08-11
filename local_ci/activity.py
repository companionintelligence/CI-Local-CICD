"""Has this workflow actually run lately? The third axis, and the missing one.

``billing.py`` answers "would a run cost money" and ``workflows.py`` answers
"can a run start without a human". Neither answers "did one happen", and the
report that shipped in #9 quietly used *gating* as a stand-in for activity. It
is not one: a workflow can be ungated and idle for a year, and it can be gated
yet dispatched by hand every morning.

Run history is the only evidence that settles it, so it lives here, apart from
both other axes. Everything below the fetch boundary is pure: :func:`from_runs`
takes the API payload and returns a verdict, which is what the tests exercise.

``UNKNOWN`` is a first-class answer. Nothing here guesses when it has no data -
a missing answer that says so is worth more than a confident default, which is
the shape of the bug this module exists to close.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence

# How far back a run still counts as "this workflow is in use". Long enough to
# cover a fortnightly release train, short enough that an abandoned workflow
# falls out of it.
DEFAULT_WINDOW_DAYS = 30

ACTIVE = "active"      # ran inside the window
DORMANT = "dormant"    # run history readable, and empty inside the window
UNKNOWN = "unknown"    # never looked, or GitHub would not say


def parse_timestamp(value: str) -> Optional[datetime]:
    """Parse a GitHub ISO-8601 timestamp. Returns None rather than raising."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class RunActivity:
    """What the run history says, and whether we have any.

    ``known=False`` means "not measured" and must never be read as "idle";
    conflating the two is how a live workflow gets written off as dead.
    """

    last_run_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    runs_in_window: int = 0
    successes_in_window: int = 0
    window_days: int = DEFAULT_WINDOW_DAYS
    known: bool = True

    @classmethod
    def unknown(cls, window_days: int = DEFAULT_WINDOW_DAYS) -> "RunActivity":
        return cls(window_days=window_days, known=False)

    @property
    def state(self) -> str:
        if not self.known:
            return UNKNOWN
        return ACTIVE if self.runs_in_window else DORMANT

    @property
    def ran_recently(self) -> bool:
        return self.known and self.runs_in_window > 0

    @property
    def succeeded_recently(self) -> bool:
        return self.known and self.successes_in_window > 0

    def describe(self) -> str:
        if not self.known:
            return "run history not checked"
        if not self.runs_in_window:
            if self.last_run_at:
                return (f"no runs in {self.window_days}d "
                        f"(last {self.last_run_at.date().isoformat()})")
            return f"no runs in {self.window_days}d"
        stamp = (self.last_success_at or self.last_run_at)
        when = stamp.isoformat().replace("+00:00", "Z") if stamp else "?"
        return (f"{self.runs_in_window} run(s)/{self.window_days}d, "
                f"{self.successes_in_window} successful, last {when}")


def from_runs(
    runs: Sequence[Dict],
    now: Optional[datetime] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> RunActivity:
    """Build a verdict from ``actions/workflows/{file}/runs`` entries.

    An empty list is a real answer - GitHub said "no runs" - so it yields a
    known, dormant verdict. Only a failed lookup produces ``unknown``.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    last_run: Optional[datetime] = None
    last_success: Optional[datetime] = None
    in_window = successes = 0
    for run in runs:
        stamp = parse_timestamp(str(run.get("created_at") or ""))
        if stamp is None:
            continue
        succeeded = run.get("conclusion") == "success"
        if last_run is None or stamp > last_run:
            last_run = stamp
        if succeeded and (last_success is None or stamp > last_success):
            last_success = stamp
        if stamp >= cutoff:
            in_window += 1
            if succeeded:
                successes += 1
    return RunActivity(
        last_run_at=last_run,
        last_success_at=last_success,
        runs_in_window=in_window,
        successes_in_window=successes,
        window_days=window_days,
        known=True,
    )


def _gh_json(args: List[str]) -> Optional[object]:
    proc = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if proc.returncode or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def fetch_activity(
    repo_slug: str,
    workflow_file: str,
    branch: str = "",
    window_days: int = DEFAULT_WINDOW_DAYS,
    per_page: int = 50,
    now: Optional[datetime] = None,
    runner: Callable[[List[str]], Optional[object]] = _gh_json,
) -> RunActivity:
    """Read recent runs for one workflow. Network; injected for tests.

    Not scoped to ``branch`` by default: a workflow that fires on a release tag
    bills exactly as much as one that fires on the default branch, and filtering
    by branch would report it dormant.
    """
    path = (f"repos/{repo_slug}/actions/workflows/{workflow_file}/runs"
            f"?per_page={per_page}")
    if branch:
        path += f"&branch={branch}"
    payload = runner(["api", path])
    if not isinstance(payload, dict):
        return RunActivity.unknown(window_days)
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        return RunActivity.unknown(window_days)
    return from_runs(runs, now=now, window_days=window_days)
