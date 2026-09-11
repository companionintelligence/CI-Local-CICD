"""Executing a check plan locally."""

from __future__ import annotations

import os
import sys

from local_ci.detect import Check, RepoPlan
from local_ci.runner import run_check, run_plan, run_plans, summarize

PY = sys.executable


def py(code: str):
    return [PY, "-c", code]


def make_plan(tmp_path, checks, install=None, stack="python", manager=None):
    return RepoPlan("repo", tmp_path, stack=stack, checks=checks,
                    install=install, manager=manager)


class TestRunCheck:
    def test_success(self, tmp_path):
        plan = make_plan(tmp_path, [])
        result = run_check(Check("test", py("pass")), plan, timeout=30)
        assert result.status == "pass"
        assert result.returncode == 0
        assert result.ok

    def test_failure_captures_output_tail(self, tmp_path):
        plan = make_plan(tmp_path, [])
        code = "import sys; print('boom detail'); sys.exit(3)"
        result = run_check(Check("test", py(code)), plan, timeout=30)
        assert result.status == "fail"
        assert result.returncode == 3
        assert "boom detail" in result.output_tail
        assert not result.ok

    def test_timeout(self, tmp_path):
        plan = make_plan(tmp_path, [])
        result = run_check(Check("test", py("import time; time.sleep(5)")), plan, 1)
        assert result.status == "timeout"
        assert "exceeded 1s" in result.reason
        assert not result.ok

    def test_missing_optional_tool_is_skipped(self, tmp_path):
        plan = make_plan(tmp_path, [])
        check = Check("lint", ["definitely-not-a-real-binary"], required=False)
        result = run_check(check, plan, timeout=5)
        assert result.status == "skip"
        assert "not on PATH" in result.reason
        assert result.ok

    def test_missing_required_tool_fails(self, tmp_path):
        plan = make_plan(tmp_path, [])
        check = Check("test", ["definitely-not-a-real-binary"], required=True)
        assert run_check(check, plan, timeout=5).status == "fail"

    def test_runs_in_the_repo_directory(self, tmp_path):
        (tmp_path / "marker.txt").write_text("here", encoding="utf-8")
        plan = make_plan(tmp_path, [])
        code = "import os,sys; sys.exit(0 if os.path.exists('marker.txt') else 1)"
        assert run_check(Check("test", py(code)), plan, 30).status == "pass"

    def test_ci_env_var_is_set(self, tmp_path):
        plan = make_plan(tmp_path, [])
        code = "import os,sys; sys.exit(0 if os.environ.get('CI')=='true' else 1)"
        assert run_check(Check("test", py(code)), plan, 30).status == "pass"


class TestRunPlan:
    def test_all_checks_run_when_passing(self, tmp_path):
        plan = make_plan(tmp_path, [
            Check("lint", py("pass")),
            Check("test", py("pass")),
        ])
        result = run_plan(plan)
        assert result.status == "pass"
        assert [c.status for c in result.checks] == ["pass", "pass"]

    def test_required_failure_short_circuits_the_rest(self, tmp_path):
        plan = make_plan(tmp_path, [
            Check("lint", py("import sys; sys.exit(1)")),
            Check("test", py("pass")),
            Check("build", py("pass")),
        ])
        result = run_plan(plan)
        assert result.status == "fail"
        assert [c.status for c in result.checks] == ["fail", "skip", "skip"]
        assert "earlier `lint` failed" in result.checks[1].reason

    def test_optional_failure_does_not_stop_the_run(self, tmp_path):
        plan = make_plan(tmp_path, [
            Check("lint", py("import sys; sys.exit(1)"), required=False),
            Check("test", py("pass")),
        ])
        result = run_plan(plan)
        assert [c.status for c in result.checks] == ["fail", "pass"]
        assert result.status == "fail"  # still reported, just not short-circuited

    def test_only_filter_selects_checks(self, tmp_path):
        plan = make_plan(tmp_path, [
            Check("lint", py("pass")),
            Check("test", py("pass")),
            Check("build", py("pass")),
        ])
        result = run_plan(plan, only=["test"])
        assert [c.name for c in result.checks] == ["test"]

    def test_install_runs_first_when_requested(self, tmp_path):
        plan = make_plan(
            tmp_path,
            [Check("test", py("pass"))],
            install=Check("install", py("pass")),
        )
        result = run_plan(plan, install=True)
        assert [c.name for c in result.checks] == ["install", "test"]

    def test_install_is_skipped_by_default(self, tmp_path):
        plan = make_plan(
            tmp_path,
            [Check("test", py("pass"))],
            install=Check("install", py("pass")),
        )
        assert [c.name for c in run_plan(plan).checks] == ["test"]

    def test_node_repo_installs_when_node_modules_is_absent(self, tmp_path):
        plan = make_plan(
            tmp_path,
            [Check("test", py("pass"))],
            install=Check("install", py("pass")),
            stack="node",
        )
        assert run_plan(plan).checks[0].name == "install"

    def test_node_repo_installs_when_node_modules_is_stale(self, tmp_path):
        """The tree exists but predates package.json - install before checking."""
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        marker = tmp_path / "node_modules" / ".modules.yaml"
        marker.parent.mkdir()
        marker.write_text("", encoding="utf-8")
        os.utime(marker, (1_000, 1_000))
        os.utime(marker.parent, (1_000, 1_000))
        os.utime(tmp_path / "package.json", (2_000, 2_000))

        plan = make_plan(
            tmp_path,
            [Check("test", py("pass"))],
            install=Check("install", py("pass")),
            stack="node",
            manager="pnpm",
        )
        assert [c.name for c in run_plan(plan).checks] == ["install", "test"]

    def test_node_repo_skips_install_when_the_tree_is_current(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        marker = tmp_path / "node_modules" / ".modules.yaml"
        marker.parent.mkdir()
        marker.write_text("", encoding="utf-8")
        os.utime(tmp_path / "package.json", (1_000, 1_000))
        os.utime(marker, (5_000, 5_000))

        plan = make_plan(
            tmp_path,
            [Check("test", py("pass"))],
            install=Check("install", py("pass")),
            stack="node",
            manager="pnpm",
        )
        assert [c.name for c in run_plan(plan).checks] == ["test"]

    def test_failed_install_skips_everything_after(self, tmp_path):
        plan = make_plan(
            tmp_path,
            [Check("test", py("pass"))],
            install=Check("install", py("import sys; sys.exit(1)")),
        )
        result = run_plan(plan, install=True)
        assert [c.status for c in result.checks] == ["fail", "skip"]


class TestRunPlansAndSummary:
    def test_results_keep_input_order(self, tmp_path):
        plans = []
        for name in ("a", "b", "c"):
            root = tmp_path / name
            root.mkdir()
            plans.append(RepoPlan(name, root, "python",
                                  checks=[Check("test", py("pass"))]))
        results = run_plans(plans, jobs=3)
        assert [r.repo for r in results] == ["a", "b", "c"]

    def test_serial_mode(self, tmp_path):
        plan = make_plan(tmp_path, [Check("test", py("pass"))])
        assert len(run_plans([plan], jobs=1)) == 1

    def test_summary_counts_and_serialises(self, tmp_path):
        good = make_plan(tmp_path, [Check("test", py("pass"))])
        bad = RepoPlan("bad", tmp_path, "python",
                       checks=[Check("test", py("import sys; sys.exit(1)"))])
        summary = summarize(run_plans([good, bad], jobs=2))
        assert summary["repos"] == 2
        assert summary["passed"] == 1
        assert summary["failed"] == 1
        import json

        json.dumps(summary)  # must be JSON-serialisable for --report


def test_check_that_reads_stdin_does_not_hang(tmp_path):
    """Without stdin=DEVNULL an interactive tool blocks until the timeout."""
    plan = make_plan(tmp_path, [])
    check = Check("test", py("import sys; sys.stdin.read(); print('ok')"))
    result = run_check(check, plan, timeout=10)
    assert result.status != "timeout"
