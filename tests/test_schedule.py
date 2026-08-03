"""Installing the drift watch as a launchd agent / systemd timer."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_ci import schedule
from local_ci.schedule import LABEL, install, launchd_plist, systemd_units, uninstall


class FakeRun:
    def __init__(self, failures=()):
        self.calls = []
        self.failures = set(failures)

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))

        class Result:
            returncode = 1 if any(f in argv for f in self.failures) else 0
            stdout = ""
            stderr = "boom" if returncode else ""

        return Result()

    def ran(self, *needles):
        return any(all(n in call for n in needles) for call in self.calls)


@pytest.fixture
def redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "LAUNCHD_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(schedule, "SYSTEMD_DIR", tmp_path / "systemd")
    return tmp_path


class TestPlistGeneration:
    def test_plist_invokes_the_drift_check(self):
        text = launchd_plist(Path("/bin/ci-local"), Path("/ws"),
                             Path("/tmp/l.log"), 9)
        assert "<string>verify</string>" in text
        assert "<string>--check-drift</string>" in text
        assert "<string>--notify</string>" in text
        assert LABEL in text

    def test_plist_pins_the_workspace(self):
        text = launchd_plist(Path("/bin/ci-local"), Path("/Users/liam/devel/ci"),
                             Path("/tmp/l.log"), 9)
        assert "<string>/Users/liam/devel/ci</string>" in text

    def test_plist_sets_the_hour(self):
        assert "<integer>6</integer>" in launchd_plist(
            Path("/b"), Path("/w"), Path("/l"), 6)

    def test_plist_does_not_run_at_load(self):
        """A watch that fires on every login is noise, not signal."""
        assert "<key>RunAtLoad</key>\n  <false/>" in launchd_plist(
            Path("/b"), Path("/w"), Path("/l"), 9)

    def test_plist_carries_path_for_gh(self):
        # gh and git must resolve; launchd's default PATH is minimal.
        assert "<key>PATH</key>" in launchd_plist(
            Path("/b"), Path("/w"), Path("/l"), 9)


class TestSystemdGeneration:
    def test_timer_is_daily_at_the_given_hour(self):
        _, timer = systemd_units(Path("/b"), Path("/w"), 7)
        assert "OnCalendar=*-*-* 07:00:00" in timer
        assert "Persistent=true" in timer

    def test_service_runs_the_drift_check(self):
        service, _ = systemd_units(Path("/b"), Path("/w"), 9)
        assert "verify --check-drift --notify" in service


class TestInstall:
    def test_darwin_writes_and_bootstraps(self, redirect):
        run = FakeRun()
        result = install(Path("/bin/ci-local"), Path("/ws"),
                         system="Darwin", log=redirect / "d.log", run=run)
        assert result.installed
        assert result.paths[0].exists()
        assert run.ran("bootout")   # reinstall-safe
        assert run.ran("bootstrap")

    def test_darwin_reports_bootstrap_failure(self, redirect):
        run = FakeRun(failures=["bootstrap"])
        result = install(Path("/bin/ci-local"), Path("/ws"),
                         system="Darwin", log=redirect / "d.log", run=run)
        assert not result.installed
        assert "bootstrap failed" in result.message
        # The plist is still on disk so the user can inspect it.
        assert result.paths[0].exists()

    def test_linux_writes_both_units_and_enables(self, redirect):
        run = FakeRun()
        result = install(Path("/bin/ci-local"), Path("/ws"), system="Linux",
                         log=redirect / "d.log", run=run)
        assert result.installed
        assert all(p.exists() for p in result.paths)
        assert run.ran("daemon-reload")
        assert run.ran("enable", "--now")

    def test_unsupported_platform(self, redirect):
        result = install(Path("/b"), Path("/w"), system="Windows",
                         log=redirect / "l", run=FakeRun())
        assert not result.installed
        assert "unsupported" in result.message

    def test_reinstall_overwrites_cleanly(self, redirect):
        run = FakeRun()
        install(Path("/bin/a"), Path("/ws"), system="Darwin",
                log=redirect / "d.log", run=run)
        install(Path("/bin/b"), Path("/ws"), system="Darwin",
                log=redirect / "d.log", run=run)
        plist = (redirect / "LaunchAgents" / f"{LABEL}.plist").read_text()
        assert "/bin/b" in plist
        assert "/bin/a" not in plist


class TestUninstall:
    def test_darwin_removes_the_plist(self, redirect):
        run = FakeRun()
        install(Path("/b"), Path("/w"), system="Darwin",
                log=redirect / "d.log", run=run)
        result = uninstall(system="Darwin", run=run)
        assert result.message == "removed"
        assert not result.paths[0].exists()

    def test_uninstall_when_absent_is_not_an_error(self, redirect):
        result = uninstall(system="Darwin", run=FakeRun())
        assert result.message == "was not installed"

    def test_linux_disables_the_timer(self, redirect):
        run = FakeRun()
        install(Path("/b"), Path("/w"), system="Linux",
                log=redirect / "d.log", run=run)
        uninstall(system="Linux", run=run)
        assert run.ran("disable", "--now")


class TestNotify:
    def test_darwin_uses_osascript(self):
        run = FakeRun()
        assert schedule.notify("t", "m", system="Darwin", run=run)
        assert run.ran("osascript")

    def test_quotes_are_neutralised(self):
        run = FakeRun()
        schedule.notify("t", 'has "quotes"', system="Darwin", run=run)
        assert '"' not in run.calls[0][-1].split("notification ")[1].split(" with")[0][1:-1]

    def test_unsupported_platform_is_silent(self):
        assert not schedule.notify("t", "m", system="Windows", run=FakeRun())
