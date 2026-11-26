"""The spec slot: checks read from a repo's ``.agent-configs/specs.toml``."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from local_ci import runner
from local_ci.cli import describe_check, main
from local_ci.detect import Check, RepoPlan, plan_for
from local_ci.docs import render_repo
from local_ci.policy import RepoOverride
from local_ci.runner import run_plan, run_plans, wanted
from local_ci.specs import REGISTRY_FILE, load
from local_ci.verify import RemoteWorkflow, RepoAudit

from .gitrepo import make_checkouts, requires_git

PY = sys.executable


def toml_argv(code: str) -> str:
    """A TOML array that runs ``code`` with this interpreter."""
    return json.dumps([PY, "-c", code])


PASS = toml_argv("pass")
FAIL = toml_argv("import sys; sys.exit(3)")

VALID = """\
version = 1
repo = "Demo"

[[spec]]
id = "action-ledger"
title = "Action ledger"
kind = "state-machine"
tool = "quint"
status = "checked"
gate = "required"
artefacts = ["spec/quint/ledger.qnt"]
check = ["npm", "run", "--silent", "spec:ledger"]

[[spec]]
id = "sync-containment"
title = "Sync stays inside the target"
kind = "invariant"
tool = "lemmascript"
status = "draft"
artefacts = ["src/sync.ts"]
check = ["npx", "lsc", "check", "src/sync.ts"]
workdir = "tools"
timeout_seconds = 60

[[spec]]
id = "consent-flow"
title = "Consent flow"
kind = "ui-flow"
tool = "bombadil"
status = "planned"
"""


def write_registry(root, text):
    path = root / REGISTRY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def entry(ident="demo", status="checked", gate="advisory", check=PASS, extra=""):
    return (f'\n[[spec]]\nid = "{ident}"\ntitle = "t"\nkind = "invariant"\n'
            f'tool = "property"\nstatus = "{status}"\ngate = "{gate}"\n'
            f"artefacts = [\"a\"]\ncheck = {check}\n{extra}")


def repo_with(tmp_path, *entries, name="Demo"):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return write_registry(root, 'version = 1\nrepo = "Demo"\n' + "".join(entries))


def spec_checks(plan):
    return [c for c in plan.checks if c.name.startswith("spec")]


class TestValidRegistry:
    def test_each_runnable_entry_becomes_a_check(self, tmp_path):
        root = write_registry(tmp_path / "Demo", VALID)
        plan = plan_for("Demo", root)
        assert [c.name for c in plan.checks] == ["spec:action-ledger", "spec:sync-containment"]

        ledger, sync = plan.checks
        assert ledger.argv == ["npm", "run", "--silent", "spec:ledger"]
        assert ledger.cwd == root
        assert ledger.timeout == 300
        assert sync.cwd == root / "tools"
        assert sync.timeout == 60

    def test_required_gates_and_advisory_does_not(self, tmp_path):
        root = write_registry(tmp_path / "Demo", VALID)
        ledger, sync = plan_for("Demo", root).checks
        assert (ledger.required, ledger.advisory) == (True, False)
        assert (sync.required, sync.advisory) == (False, True)

    def test_gate_defaults_to_advisory(self, tmp_path):
        root = repo_with(tmp_path, entry().replace('gate = "advisory"\n', ""))
        (check,) = plan_for("Demo", root).checks
        assert check.advisory and not check.required

    def test_planned_entries_are_not_run_but_are_counted(self, tmp_path):
        root = repo_with(
            tmp_path,
            entry("modelled", status="planned"),  # a check is allowed, never run
            entry("target", status="planned").replace(f"check = {PASS}\n", ""),
            entry("real"),
        )
        plan = plan_for("Demo", root)
        assert [c.name for c in plan.checks] == ["spec:real"]
        assert f"2 planned spec(s) in {REGISTRY_FILE}, not run" in plan.notes

    def test_empty_registry_adds_nothing(self, tmp_path):
        root = repo_with(tmp_path)
        plan = plan_for("Demo", root)
        assert plan.checks == [] and plan.notes == ["no recognised project manifest"]

    def test_no_registry_adds_nothing(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "x", "scripts": {"test": "vitest"}}), encoding="utf-8")
        assert [c.name for c in plan_for("x", tmp_path).checks] == ["test"]

    def test_specs_follow_the_detected_checks(self, tmp_path):
        root = repo_with(tmp_path, entry())
        (root / "package.json").write_text(
            json.dumps({"name": "x", "scripts": {"lint": "eslint .", "test": "vitest"}}),
            encoding="utf-8")
        assert [c.name for c in plan_for("Demo", root).checks] == ["lint", "test", "spec:demo"]

    def test_unknown_keys_are_left_to_the_full_schema(self, tmp_path):
        root = repo_with(tmp_path, entry(extra='colour = "blue"\n'))
        assert [c.name for c in plan_for("Demo", root).checks] == ["spec:demo"]


class TestRepoWithOnlyARegistry:
    def test_it_is_runnable(self, tmp_path):
        root = write_registry(tmp_path / "Docs", VALID)
        plan = plan_for("Docs", root)
        assert plan.stack == "unknown"
        assert plan.runnable
        assert "no recognised project manifest" in plan.notes

    def test_plan_lists_the_checks(self, tmp_path, capsys):
        write_registry(tmp_path / "Docs", VALID)
        (tmp_path / "Docs" / ".git").mkdir()
        assert main(["--workspace", str(tmp_path), "--policy", str(tmp_path / "none.toml"),
                     "plan", "--runnable-only"]) == 0
        out = capsys.readouterr().out
        assert "Docs  [unknown]" in out
        assert "spec:action-ledger npm run --silent spec:ledger\n" in out
        assert "spec:sync-containment npx lsc check src/sync.ts  (advisory, in tools)" in out

    def test_the_hook_is_offered_to_it(self, tmp_path, capsys):
        write_registry(tmp_path / "Docs", VALID)
        (tmp_path / "Docs" / ".git" / "hooks").mkdir(parents=True)
        main(["--workspace", str(tmp_path), "--policy", str(tmp_path / "none.toml"),
              "hooks", "install"])
        out = capsys.readouterr().out
        assert "installed Docs" in out
        assert "no local checks detected" not in out


INVALID = {
    "not TOML": ('version = 1\n[[spec]\n', "not valid TOML"),
    "no version": ('repo = "Demo"\n', "version: missing"),
    "version as a string": ('version = "1"\n', 'version: must be 1, not "1"'),
    "version as a bool": ("version = true\n", "version: must be 1, not true"),
    "version 2": ("version = 2\n", "version: must be 1, not 2"),
    "spec as one table": ('version = 1\n[spec]\nid = "a"\n', "spec: must be an array of tables"),
    "id not kebab case": ("version = 1\n" + entry("Action_Ledger"),
                          'spec[0].id: must be a kebab-case id such as action-ledger, '
                          'not "Action_Ledger"'),
    "id missing": ("version = 1\n" + entry().replace('id = "demo"\n', ""), "spec[0].id: missing"),
    "duplicate id": ("version = 1\n" + entry("a") + entry("b") + entry("a"),
                     'spec[2].id: duplicate id "a": spec[0] already uses it'),
    "unknown status": ("version = 1\n" + entry(status="done"),
                       'spec[0].status: must be one of planned, draft, checked, '
                       'conformance, not "done"'),
    "status missing": ("version = 1\n" + entry().replace('status = "checked"\n', ""),
                       "spec[0].status: missing"),
    "unknown gate": ("version = 1\n" + entry(gate="blocking"),
                     'spec[0].gate: must be advisory or required, not "blocking"'),
    "required draft": ("version = 1\n" + entry(status="draft", gate="required"),
                       'spec[0].gate: "required" needs status "checked" or '
                       '"conformance", not "draft"'),
    "required planned": ("version = 1\n" + entry(status="planned", gate="required"),
                         'not "planned"'),
    "check missing": ("version = 1\n" + entry().replace(f"check = {PASS}\n", ""),
                      'spec[0].check: required unless status is "planned"'),
    "check as a string": ("version = 1\n" + entry(check='"npm test"'),
                          'spec[0].check: must be an argv array of non-empty strings, '
                          'not "npm test"'),
    "check empty": ("version = 1\n" + entry(check="[]"), "spec[0].check: must be an argv array"),
    "check with an empty arg": ("version = 1\n" + entry(check='["npm", ""]'),
                                "spec[0].check: must be an argv array"),
    "absolute workdir": ("version = 1\n" + entry(extra='workdir = "/tmp"\n'),
                         "spec[0].workdir: must be relative to the repository root"),
    "workdir escapes": ("version = 1\n" + entry(extra='workdir = "a/../../b"\n'),
                        "spec[0].workdir: must not contain a '..' segment"),
    "zero timeout": ("version = 1\n" + entry(extra="timeout_seconds = 0\n"),
                     "spec[0].timeout_seconds: must be a whole number from 1 to 86400, not 0"),
    "float timeout": ("version = 1\n" + entry(extra="timeout_seconds = 1.5\n"),
                      "not 1.5"),
}


class TestInvalidRegistry:
    """An unusable registry fails the slot visibly instead of being skipped."""

    @pytest.mark.parametrize("case", INVALID)
    def test_becomes_one_failing_check(self, tmp_path, case):
        text, problem = INVALID[case]
        root = write_registry(tmp_path / "Demo", text)
        plan = plan_for("Demo", root)
        (check,) = spec_checks(plan)
        assert check.name == "spec"
        assert check.required and not check.advisory
        assert check.error.startswith(f"{REGISTRY_FILE} is invalid: ")
        assert problem in check.error

    def test_every_problem_is_reported(self, tmp_path):
        root = repo_with(tmp_path, entry(status="draft", gate="required"),
                         entry("Bad", check='"x"'))
        (check,) = plan_for("Demo", root).checks
        assert "spec[0].gate" in check.error
        assert "spec[1].id" in check.error and "spec[1].check" in check.error

    def test_valid_entries_do_not_run_beside_an_invalid_one(self, tmp_path):
        root = repo_with(tmp_path, entry("fine"), entry("broken", gate="nope"))
        assert [c.name for c in plan_for("Demo", root).checks] == ["spec"]

    def test_a_registry_that_cannot_be_read_is_invalid(self, tmp_path):
        (tmp_path / REGISTRY_FILE).mkdir(parents=True)  # a directory, not a file
        assert load(tmp_path).problems == ["cannot be read: Is a directory"]

    def test_the_run_fails_and_says_why(self, tmp_path):
        root = repo_with(tmp_path, entry(status="draft", gate="required"))
        result = run_plan(plan_for("Demo", root))
        assert result.status == "fail"
        (outcome,) = result.checks
        assert outcome.status == "fail"
        assert '"required" needs status "checked"' in outcome.reason

    def test_plan_shows_the_problem(self, tmp_path, capsys):
        repo_with(tmp_path, entry(status="draft", gate="required"))
        (tmp_path / "Demo" / ".git").mkdir()
        main(["--workspace", str(tmp_path), "--policy", str(tmp_path / "none.toml"), "plan"])
        out = capsys.readouterr().out
        assert f"    spec       cannot run: {REGISTRY_FILE} is invalid: spec[0].gate" in out


class TestGates:
    """A required spec gates the run; advisory and draft ones only report."""

    def run(self, tmp_path, *entries):
        root = repo_with(tmp_path, *entries)
        return run_plan(plan_for("Demo", root))

    def test_failing_required_fails_the_repo(self, tmp_path):
        result = self.run(tmp_path, entry("gate", gate="required", check=FAIL))
        assert result.status == "fail"
        assert [c.status for c in result.checks] == ["fail"]

    def test_failing_advisory_warns_and_passes(self, tmp_path):
        result = self.run(tmp_path, entry("hint", check=FAIL))
        assert result.status == "pass"
        (outcome,) = result.checks
        assert outcome.status == "warn"
        assert outcome.returncode == 3

    def test_failing_draft_warns_and_passes(self, tmp_path):
        result = self.run(tmp_path, entry("wip", status="draft", check=FAIL))
        assert result.status == "pass"
        assert result.checks[0].status == "warn"

    def test_advisory_failure_does_not_stop_a_later_required_check(self, tmp_path):
        result = self.run(tmp_path, entry("hint", check=FAIL),
                          entry("gate", gate="required", check=PASS))
        assert [c.status for c in result.checks] == ["warn", "pass"]

    def test_missing_tool_warns_when_advisory_and_fails_when_required(self, tmp_path):
        missing = '["definitely-not-a-real-binary"]'
        result = self.run(tmp_path, entry("hint", check=missing),
                          entry("gate", gate="required", check=missing))
        assert [c.status for c in result.checks] == ["warn", "fail"]
        assert "not on PATH" in result.checks[0].reason

    def test_timeout_seconds_caps_the_check(self, tmp_path):
        slow = toml_argv("import time; time.sleep(5)")
        result = self.run(tmp_path, entry("slow", check=slow, extra="timeout_seconds = 1\n"),
                          entry("slower", gate="required", check=slow,
                                extra="timeout_seconds = 1\n"))
        assert [c.status for c in result.checks] == ["warn", "timeout"]
        assert result.checks[1].reason == "exceeded 1s"

    def test_quiet_run_still_prints_the_warning(self, tmp_path, capsys):
        repo_with(tmp_path, entry("hint", check=FAIL))
        (tmp_path / "Demo" / ".git").mkdir()
        code = main(["--workspace", str(tmp_path), "--policy", str(tmp_path / "none.toml"),
                     "run", "--quiet", "--jobs", "1"])
        out = capsys.readouterr().out
        assert code == 0
        assert "WARN spec:hint" in out
        assert "1/1 repos passed" in out


def alive(pid):
    """Whether ``pid`` is running; a zombie waiting to be reaped is not."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="needs process groups")
class TestChildProcesses:
    """A spec check's own children stop with it, on a timeout and on Ctrl-C.

    A tool such as `quint verify` starts a server on a fixed port; one left
    running breaks the next run on that port.
    """

    @pytest.fixture
    def child(self, tmp_path):
        """A check that starts a long-lived child, and the file its pid goes to."""
        pidfile = tmp_path / "child.pid"
        code = ("import subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"open({str(pidfile)!r}, 'w').write(str(child.pid)); time.sleep(60)")
        yield pidfile, toml_argv(code)
        runner._STOPPING.clear()
        if pidfile.exists():  # so that a failing test leaves nothing behind
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass

    @staticmethod
    def gone(pid, wait=5.0):
        deadline = time.monotonic() + wait
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not alive(pid)

    @staticmethod
    def once_running(pidfile, action):
        def wait_then_act():
            deadline = time.monotonic() + 20
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.2)  # the pid is written; let the write finish
            action()
        threading.Thread(target=wait_then_act, daemon=True).start()

    def test_a_timeout_stops_the_checks_children(self, tmp_path, child):
        pidfile, check = child
        root = repo_with(tmp_path, entry("server", gate="required", check=check,
                                         extra="timeout_seconds = 3\n"))
        started = time.monotonic()
        result = run_plan(plan_for("Demo", root))
        assert time.monotonic() - started < 20  # not the 60 s the check would take
        assert result.checks[0].status == "timeout"
        assert self.gone(int(pidfile.read_text()))

    def test_an_interrupt_stops_them_and_starts_nothing_more(self, tmp_path, child):
        pidfile, check = child
        root = repo_with(tmp_path, entry("server", check=check), entry("next", check=PASS))
        self.once_running(pidfile, runner._interrupt)
        started = time.monotonic()
        result = run_plan(plan_for("Demo", root))
        assert time.monotonic() - started < 20
        assert [c.name for c in result.checks] == ["spec:server"]
        assert self.gone(int(pidfile.read_text()))

    @pytest.mark.skipif(signal.getsignal(signal.SIGINT) is not signal.default_int_handler,
                        reason="needs Python's own SIGINT handler")
    @pytest.mark.parametrize("jobs", [1, 2])
    def test_ctrl_c_does_not_wait_for_them(self, tmp_path, child, jobs):
        """With jobs > 1 (the pre-push hook's run), Ctrl-C lands in the main
        thread while a worker waits on the check."""
        pidfile, check = child
        root = repo_with(tmp_path, entry("server", check=check))
        self.once_running(pidfile, lambda: os.kill(os.getpid(), signal.SIGINT))
        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            run_plans([plan_for("Demo", root)], jobs=jobs)
        assert time.monotonic() - started < 20
        assert self.gone(int(pidfile.read_text()))


class TestWhereSpecsRun:
    def test_from_the_repo_root_by_default(self, tmp_path):
        root = repo_with(tmp_path, entry(check=toml_argv(
            "import os, sys; sys.exit(0 if os.path.isdir('.agent-configs') else 1)")))
        assert run_plan(plan_for("Demo", root)).checks[0].status == "pass"

    def test_from_the_root_even_when_policy_moves_detection(self, tmp_path):
        root = repo_with(tmp_path, entry(gate="required", check=toml_argv(
            "import os, sys; sys.exit(0 if os.path.isdir('.agent-configs') else 1)")))
        (root / "backend").mkdir()
        plan = plan_for("Demo", root, RepoOverride(workdir="backend"))
        assert plan.path == root / "backend"
        assert describe_check(plan.checks[0], plan).endswith("  (in ..)")
        assert run_plan(plan).checks[0].status == "pass"

    def test_from_the_entry_workdir(self, tmp_path):
        root = repo_with(tmp_path, entry(gate="required", extra='workdir = "tools"\n',
                                         check=toml_argv(
            "import os, sys; sys.exit(0 if os.path.basename(os.getcwd()) == 'tools' else 1)")))
        (root / "tools").mkdir()
        assert run_plan(plan_for("Demo", root)).checks[0].status == "pass"

    def test_a_relative_program_is_found_in_the_workdir(self, tmp_path, monkeypatch):
        root = repo_with(tmp_path, entry(gate="required", check='["./check.sh"]',
                                         extra='workdir = "tools"\n'))
        script = root / "tools" / "check.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)  # not where the check runs
        assert run_plan(plan_for("Demo", root)).checks[0].status == "pass"

    def test_a_policy_skip_drops_them_too(self, tmp_path):
        root = repo_with(tmp_path, entry())
        plan = plan_for("Demo", root, RepoOverride(skip=True))
        assert plan.checks == []

    def test_policy_commands_keep_them(self, tmp_path):
        root = repo_with(tmp_path, entry())
        override = RepoOverride(commands=[{"name": "test", "run": ["make", "test"]}])
        assert [c.name for c in plan_for("Demo", root, override).checks] == ["test", "spec:demo"]


class TestOnlySpec:
    def plan(self, tmp_path):
        return RepoPlan("Demo", tmp_path, "node", checks=[
            Check("lint", [PY, "-c", "pass"]),
            Check("test", [PY, "-c", "pass"]),
            Check("test:@demo/web", [PY, "-c", "pass"]),
            Check("spec:a", [PY, "-c", "pass"], required=False, advisory=True),
            Check("spec:b", [PY, "-c", "pass"]),
        ])

    def names(self, tmp_path, only):
        return [c.name for c in run_plan(self.plan(tmp_path), only=only).checks]

    def test_spec_selects_every_spec_check(self, tmp_path):
        assert self.names(tmp_path, ["spec"]) == ["spec:a", "spec:b"]

    def test_spec_combines_with_exact_names(self, tmp_path):
        assert self.names(tmp_path, ["lint", "test", "spec"]) == [
            "lint", "test", "spec:a", "spec:b"]

    def test_other_names_still_match_exactly(self, tmp_path):
        assert self.names(tmp_path, ["test"]) == ["test"]
        assert self.names(tmp_path, ["spec:b"]) == ["spec:b"]

    def test_spec_selects_the_invalid_registry_check(self):
        assert wanted("spec", ["spec"])
        assert not wanted("spec", ["lint"])
        assert not wanted("specs", ["spec"])

    def test_on_the_command_line(self, tmp_path, capsys):
        root = repo_with(tmp_path / "ws", entry("a"), entry("b", gate="required"))
        (root / ".git").mkdir()
        policy = tmp_path / "policy.toml"  # a red lint that --only spec leaves out
        policy.write_text(f'[checks.Demo]\ncommands = [{{ name = "lint", run = {FAIL} }}]\n',
                          encoding="utf-8")
        report = tmp_path / "report.json"
        code = main(["--workspace", str(tmp_path / "ws"), "--policy", str(policy),
                     "run", "--only", "spec", "--jobs", "1", "--report", str(report)])
        capsys.readouterr()
        assert code == 0
        (repo,) = json.loads(report.read_text(encoding="utf-8"))["results"]
        assert [c["name"] for c in repo["checks"]] == ["spec:a", "spec:b"]


@requires_git
def test_a_worktree_push_reads_the_worktrees_own_registry(tmp_path, capsys):
    """The hook checks the tree being pushed, so its registry is that tree's."""
    co = make_checkouts(tmp_path / "ws")
    write_registry(co.worktree, 'version = 1\nrepo = "Demo"\n' + entry("only-here"))
    argv = ["--workspace", str(co.main.parent), "--policy", str(tmp_path / "none.toml"),
            "plan", "--path"]
    main(argv + [str(co.worktree)])
    assert "spec:only-here" in capsys.readouterr().out
    main(argv + [str(co.main)])
    assert "spec:only-here" not in capsys.readouterr().out


class TestDocs:
    def plan(self, root):
        return RepoPlan("Demo", root, "node", manager="npm",
                        install=Check("install", ["npm", "ci"]),
                        checks=[Check("test", ["npm", "run", "--silent", "test"]),
                                Check("spec:a", ["npm", "run", "spec:a"], cwd=root),
                                Check("spec:b", ["quint", "test", "b.qnt"], required=False,
                                      advisory=True, cwd=root / "spec" / "quint")])

    def render(self, plan, audit=None):
        audit = audit or RepoAudit("Demo", "main", workflows=[
            RemoteWorkflow(name="ci.yml", gated=True, triggers=["workflow_dispatch"])])
        return "\n".join(render_repo(audit, plan, "Demo"))

    def test_lists_each_spec_check_with_its_gate(self, tmp_path):
        out = self.render(self.plan(tmp_path))
        assert "### Spec checks" in out
        assert "`ci-local run --repo Demo --only spec`" in out
        assert "- `spec:a` (required): `npm run spec:a`" in out
        assert "- `spec:b` (advisory, in `spec/quint`): `quint test b.qnt`" in out

    def test_spec_checks_are_not_repeated_as_plain_commands(self, tmp_path):
        out = self.render(self.plan(tmp_path))
        equivalent = out[out.index("Equivalent to:"):out.index("### Spec checks")]
        assert "npm run --silent test" in equivalent
        assert "spec:a" not in equivalent

    def test_a_repo_with_only_spec_checks(self, tmp_path):
        plan = RepoPlan("Demo", tmp_path, "unknown",
                        checks=[Check("spec:a", ["make", "spec"], cwd=tmp_path)])
        out = self.render(plan)
        assert "Equivalent to:" not in out
        assert "- `spec:a` (required): `make spec`" in out

    def test_an_invalid_registry(self, tmp_path):
        plan = RepoPlan("Demo", tmp_path, "unknown", checks=[
            Check("spec", [], error=f"{REGISTRY_FILE} is invalid: version: missing")])
        out = self.render(plan)
        assert f"- `spec` fails without running: {REGISTRY_FILE} is invalid" in out

    def test_listed_when_the_workflows_cannot_be_read(self, tmp_path):
        audit = RepoAudit("Demo", "main", error="no .github/workflows on default branch")
        out = self.render(self.plan(tmp_path), audit)
        assert "_Could not read workflows" in out
        assert "- `spec:a` (required)" in out

    def test_no_section_without_specs(self, tmp_path):
        plan = RepoPlan("Demo", tmp_path, "node", checks=[Check("test", ["npm", "test"])])
        assert "Spec checks" not in self.render(plan)

    def test_paths_stay_relative(self, tmp_path):
        out = self.render(self.plan(tmp_path))
        assert str(tmp_path) not in out


def test_plan_marks_advisory_optional_and_where(tmp_path):
    plan = RepoPlan("Demo", tmp_path, "node")
    tools = tmp_path / "tools"
    assert describe_check(Check("spec:a", ["x"], required=False, advisory=True), plan) == \
        "x  (advisory)"
    assert describe_check(Check("build", ["x"], required=False), plan) == "x  (optional)"
    assert describe_check(Check("spec:b", ["x"], cwd=tools), plan) == "x  (in tools)"
    assert describe_check(Check("spec:c", ["x"], cwd=plan.path), plan) == "x"
    assert describe_check(Check("spec", [], error="broken"), plan) == "cannot run: broken"
