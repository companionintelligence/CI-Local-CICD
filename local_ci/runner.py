"""Execute a repo's check plan on local hardware.

Checks inside one repo run in order (later ones assume the install step ran);
repos run concurrently, since they are independent. Output is captured rather
than streamed so that parallel runs do not interleave into noise - the tail of
a failing command is what you need, and that is what gets reported.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .detect import Check, RepoPlan, git_env, install_state
from .specs import SLOT, in_slot

DEFAULT_TIMEOUT = 1800  # 30 min; the longest hosted job in the report was ~25
TAIL_LINES = 40

# A spec check runs in a session of its own, so that a timeout stops the tool's
# children as well as the tool: `quint verify` starts an Apalache server on a
# fixed port, and one left running breaks the next run. Ctrl-C from the
# terminal does not reach such a session, so `run_plans` passes it on. Other
# checks stay in ci-local's process group, where Ctrl-C reaches them and they
# keep the terminal.
_LOCK = threading.Lock()
_SESSIONS: Set[subprocess.Popen] = set()
_STOPPING = threading.Event()


@dataclass
class CheckResult:
    name: str
    command: str
    status: str  # "pass" | "fail" | "skip" | "timeout" | "warn"
    duration: float = 0.0
    returncode: Optional[int] = None
    output_tail: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        # "warn" is an advisory check that failed, timed out or could not run.
        return self.status in ("pass", "skip", "warn")


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
    """Run one command in the repo, capturing its output.

    An advisory check that fails, times out or cannot find its tool is
    reported as "warn": shown, never failing the repo, and never read as a pass.
    """
    if check.error:
        return CheckResult(check.name, check.display, "fail", reason=check.error)
    result = _execute(check, plan, timeout)
    if check.advisory and result.status in ("fail", "timeout", "skip"):
        result.status = "warn"
    return result


def _execute(check: Check, plan: RepoPlan, timeout: int) -> CheckResult:
    command = check.display
    cwd = check.cwd or plan.path
    if check.timeout is not None:
        timeout = min(timeout, check.timeout)
    program = check.argv[0]
    # A relative path names a file in the check's directory, not in whatever
    # directory ci-local itself was started from.
    if os.sep in program and not os.path.isabs(program):
        program = os.path.join(str(cwd), program)
    if shutil.which(program) is None:
        return CheckResult(
            check.name,
            command,
            "skip" if not check.required else "fail",
            reason=f"`{check.argv[0]}` not on PATH",
        )

    # Not `os.environ` verbatim: a push from a worktree hands the hook
    # `GIT_DIR`, and any test that builds a scratch repo (`git init` in a tmp
    # dir, then `git config user.email ...`) would write into the real repo's
    # shared config instead. Without it, git finds the repo from `plan.path`,
    # exactly as in a manual run.
    env = git_env()
    # Make local runs look like CI to the tools that care, so snapshot and
    # watch-mode defaults match what the hosted runners did.
    env.setdefault("CI", "true")
    env.setdefault("FORCE_COLOR", "0")
    # husky reinstalls itself from the `prepare` script on every install, and
    # that rewrite of `.husky/_` drops ci-local's block from the pre-push hook.
    # HUSKY=0 makes husky skip it, here and in any check that installs.
    env["HUSKY"] = "0"

    session = in_slot(check.name)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            check.argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # No stdin. A tool that stops to ask something (flutter's first-run
            # analytics consent, a package manager confirming an install) would
            # otherwise block until the timeout with no output explaining why.
            stdin=subprocess.DEVNULL,
            start_new_session=session,
        )
    except OSError as exc:
        return CheckResult(
            check.name,
            command,
            "fail",
            duration=time.monotonic() - started,
            reason=str(exc),
        )

    with proc:
        if session and not _track(proc):
            _stop(proc, session)
            return CheckResult(check.name, command, "fail", reason="interrupted")
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop(proc, session)
            return CheckResult(
                check.name,
                command,
                "timeout",
                duration=time.monotonic() - started,
                reason=f"exceeded {timeout}s",
            )
        except BaseException:
            _stop(proc, session)
            raise
        finally:
            if session:
                _untrack(proc)

    duration = time.monotonic() - started
    if proc.returncode == 0:
        return CheckResult(check.name, command, "pass", duration, 0)
    return CheckResult(
        check.name,
        command,
        "fail",
        duration,
        proc.returncode,
        output_tail=_tail((stdout or "") + (stderr or "")),
    )


def _track(proc: subprocess.Popen) -> bool:
    """Note a running spec check, unless an interrupt has already come."""
    with _LOCK:
        if _STOPPING.is_set():
            return False
        _SESSIONS.add(proc)
        return True


def _untrack(proc: subprocess.Popen) -> None:
    with _LOCK:
        _SESSIONS.discard(proc)


def _kill(proc: subprocess.Popen, session: bool) -> None:
    """SIGKILL ``proc``, and when it leads a session, every process in it."""
    killpg = getattr(os, "killpg", None)
    if session and killpg is not None:
        try:
            killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    proc.kill()  # does nothing once the process has exited


def _stop(proc: subprocess.Popen, session: bool) -> None:
    """Kill a check that ran out of time or was interrupted, and reap it.

    Its output is not read: a child that left the session could hold the pipes
    open for as long as it runs.
    """
    _kill(proc, session)
    proc.wait()


def _interrupt() -> None:
    """Kill the spec checks running now, and let no other check start."""
    with _LOCK:
        _STOPPING.set()
        running = list(_SESSIONS)
    for proc in running:
        _kill(proc, session=True)


def wanted(name: str, only: Optional[List[str]]) -> bool:
    """Whether ``--only`` selects a check.

    Names match exactly - ``test`` is not ``test:<package>`` - except in the
    spec slot. ``spec`` selects every ``spec:*`` check, and any ``spec:<id>``
    also selects ``spec``, the check an invalid registry becomes, so that a
    broken registry fails the run rather than leaving nothing selected.
    """
    if not only:
        return True
    if name in only or (SLOT in only and in_slot(name)):
        return True
    return name == SLOT and any(in_slot(selector) for selector in only)


def run_plan(
    plan: RepoPlan,
    only: Optional[List[str]] = None,
    install: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> RepoResult:
    """Run a repo's checks in order, stopping at the first required failure."""
    result = RepoResult(plan.repo, plan.stack, notes=list(plan.notes))

    queue: List[Check] = []
    # A stale tree, not just a missing one: an install that predates the current
    # package.json/lockfile makes every later check report the wrong failure.
    needs_install = install or (
        plan.stack == "node"
        and install_state(plan.path, plan.manager) is not None
    )
    if plan.install and needs_install:
        queue.append(plan.install)
    for check in plan.checks:
        if not wanted(check.name, only):
            continue
        queue.append(check)

    for check in queue:
        if _STOPPING.is_set():
            break  # interrupted: start nothing new
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
    _STOPPING.clear()
    if jobs <= 1:
        return [run_plan(p, only, install, timeout) for p in plans]
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_plan, p, only, install, timeout) for p in plans]
        try:
            return [f.result() for f in futures]
        except BaseException:
            # Ctrl-C raises here, in the main thread. The pool then waits for
            # its workers, so stop the spec checks they are waiting on, and
            # any check they would start next.
            _interrupt()
            raise


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
