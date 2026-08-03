"""Execute a repo's check plan on local hardware.

Checks inside one repo run in order (later ones assume the install step ran);
repos run concurrently, since they are independent. Output is captured rather
than streamed so that parallel runs do not interleave into noise - the tail of
a failing command is what you need, and that is what gets reported.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .detect import Check, RepoPlan

DEFAULT_TIMEOUT = 1800  # 30 min; the longest hosted job in the report was ~25
TAIL_LINES = 40


@dataclass
class CheckResult:
    name: str
    command: str
    status: str  # "pass" | "fail" | "skip" | "timeout"
    duration: float = 0.0
    returncode: Optional[int] = None
    output_tail: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("pass", "skip")


@dataclass
class RepoResult:
    repo: str
    stack: str
    checks: List[CheckResult] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return sum(c.duration for c in self.checks)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def status(self) -> str:
        if not self.checks:
            return "empty"
        return "pass" if self.ok else "fail"


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    parts = text.rstrip().splitlines()
    return "\n".join(parts[-lines:])


def run_check(check: Check, plan: RepoPlan, timeout: int) -> CheckResult:
    """Run one command in the repo, capturing its output."""
    command = check.display
    if shutil.which(check.argv[0]) is None:
        return CheckResult(
            check.name,
            command,
            "skip" if not check.required else "fail",
            reason=f"`{check.argv[0]}` not on PATH",
        )

    env = dict(os.environ)
    # Make local runs look like CI to the tools that care, so snapshot and
    # watch-mode defaults match what the hosted runners did.
    env.setdefault("CI", "true")
    env.setdefault("FORCE_COLOR", "0")

    started = time.monotonic()
    try:
        proc = subprocess.run(
            check.argv,
            cwd=str(plan.path),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            # No stdin. A tool that stops to ask something (flutter's first-run
            # analytics consent, a package manager confirming an install) would
            # otherwise block until the timeout with no output explaining why.
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            check.name,
            command,
            "timeout",
            duration=time.monotonic() - started,
            reason=f"exceeded {timeout}s",
        )
    except OSError as exc:
        return CheckResult(
            check.name,
            command,
            "fail",
            duration=time.monotonic() - started,
            reason=str(exc),
        )

    duration = time.monotonic() - started
    if proc.returncode == 0:
        return CheckResult(check.name, command, "pass", duration, 0)
    return CheckResult(
        check.name,
        command,
        "fail",
        duration,
        proc.returncode,
        output_tail=_tail((proc.stdout or "") + (proc.stderr or "")),
    )


def run_plan(
    plan: RepoPlan,
    only: Optional[List[str]] = None,
    install: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> RepoResult:
    """Run a repo's checks in order, stopping at the first required failure."""
    result = RepoResult(plan.repo, plan.stack, notes=list(plan.notes))

    queue: List[Check] = []
    needs_install = install or (
        plan.stack == "node" and not (plan.path / "node_modules").exists()
    )
    if plan.install and needs_install:
        queue.append(plan.install)
    for check in plan.checks:
        if only and check.name not in only:
            continue
        queue.append(check)

    for check in queue:
        outcome = run_check(check, plan, timeout)
        result.checks.append(outcome)
        if not outcome.ok and check.required:
            remaining = queue[queue.index(check) + 1 :]
            for skipped in remaining:
                result.checks.append(
                    CheckResult(
                        skipped.name,
                        skipped.display,
                        "skip",
                        reason=f"earlier `{check.name}` failed",
                    )
                )
            break
    return result


def run_plans(
    plans: List[RepoPlan],
    only: Optional[List[str]] = None,
    install: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    jobs: int = 4,
) -> List[RepoResult]:
    """Run several repos concurrently, preserving input order in the output."""
    if jobs <= 1:
        return [run_plan(p, only, install, timeout) for p in plans]
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_plan, p, only, install, timeout) for p in plans]
        return [f.result() for f in futures]


def summarize(results: List[RepoResult]) -> Dict[str, object]:
    """A JSON-friendly summary, for the report file and for CI of the CI."""
    return {
        "repos": len(results),
        "passed": sum(1 for r in results if r.status == "pass"),
        "failed": sum(1 for r in results if r.status == "fail"),
        "duration_seconds": round(sum(r.duration for r in results), 1),
        "results": [
            {
                "repo": r.repo,
                "stack": r.stack,
                "status": r.status,
                "duration_seconds": round(r.duration, 1),
                "notes": r.notes,
                "checks": [
                    {
                        "name": c.name,
                        "command": c.command,
                        "status": c.status,
                        "duration_seconds": round(c.duration, 1),
                        "returncode": c.returncode,
                        "reason": c.reason,
                        "output_tail": c.output_tail,
                    }
                    for c in r.checks
                ],
            }
            for r in results
        ],
    }
