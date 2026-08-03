"""Install the drift watch as a recurring local job.

Deliberately not a GitHub Actions scheduled workflow: a cron job that exists to
police Actions spend should not itself consume billed minutes, and a schedule
that lives in one repo cannot see the other thirty. This runs on hardware you
already own.

macOS gets a launchd agent, Linux a systemd user timer. Both are written to the
user scope - no sudo, nothing system-wide.
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

LABEL = "com.companionintelligence.ci-local-drift"

LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"


def launchd_plist(binary: Path, workspace: Path, log: Path, hour: int) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{binary}</string>
    <string>--workspace</string>
    <string>{workspace}</string>
    <string>verify</string>
    <string>--check-drift</string>
    <string>--notify</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>{hour}</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{log}</string>
  <key>RunAtLoad</key>
  <false/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{os.environ.get('PATH', '/usr/bin:/bin')}</string>
  </dict>
</dict>
</plist>
"""


def systemd_units(binary: Path, workspace: Path, hour: int):
    service = f"""[Unit]
Description=ci-local Actions drift watch

[Service]
Type=oneshot
ExecStart={binary} --workspace {workspace} verify --check-drift --notify
"""
    timer = f"""[Unit]
Description=Run ci-local drift watch daily

[Timer]
OnCalendar=*-*-* {hour:02d}:00:00
Persistent=true

[Install]
WantedBy=timers.target
"""
    return service, timer


@dataclass
class InstallResult:
    installed: bool
    paths: List[Path]
    message: str


def install(
    binary: Path,
    workspace: Path,
    hour: int = 9,
    log: Optional[Path] = None,
    system: Optional[str] = None,
    run=subprocess.run,
) -> InstallResult:
    system = system or platform.system()
    log = log or (Path.home() / "Library" / "Logs" / "ci-local-drift.log"
                  if system == "Darwin" else Path.home() / ".ci-local-drift.log")

    if system == "Darwin":
        LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        plist = LAUNCHD_DIR / f"{LABEL}.plist"
        plist.write_text(launchd_plist(binary, workspace, log, hour),
                         encoding="utf-8")
        # bootout first so a reinstall picks up the new plist.
        uid = os.getuid()
        run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
            capture_output=True, text=True)
        proc = run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                   capture_output=True, text=True)
        if proc.returncode:
            return InstallResult(
                False, [plist],
                f"plist written but launchctl bootstrap failed: "
                f"{proc.stderr.strip()[:200]}",
            )
        return InstallResult(True, [plist], f"daily at {hour:02d}:00, log: {log}")

    if system == "Linux":
        SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
        service_path = SYSTEMD_DIR / "ci-local-drift.service"
        timer_path = SYSTEMD_DIR / "ci-local-drift.timer"
        service, timer = systemd_units(binary, workspace, hour)
        service_path.write_text(service, encoding="utf-8")
        timer_path.write_text(timer, encoding="utf-8")
        run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
        proc = run(["systemctl", "--user", "enable", "--now",
                    "ci-local-drift.timer"], capture_output=True, text=True)
        if proc.returncode:
            return InstallResult(
                False, [service_path, timer_path],
                f"units written but enable failed: {proc.stderr.strip()[:200]}",
            )
        return InstallResult(True, [service_path, timer_path],
                             f"daily at {hour:02d}:00")

    return InstallResult(False, [], f"unsupported platform: {system}")


def uninstall(system: Optional[str] = None, run=subprocess.run) -> InstallResult:
    system = system or platform.system()
    if system == "Darwin":
        plist = LAUNCHD_DIR / f"{LABEL}.plist"
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
            capture_output=True, text=True)
        existed = plist.exists()
        plist.unlink(missing_ok=True)
        return InstallResult(False, [plist],
                             "removed" if existed else "was not installed")
    if system == "Linux":
        run(["systemctl", "--user", "disable", "--now", "ci-local-drift.timer"],
            capture_output=True, text=True)
        paths = [SYSTEMD_DIR / "ci-local-drift.service",
                 SYSTEMD_DIR / "ci-local-drift.timer"]
        existed = any(p.exists() for p in paths)
        for p in paths:
            p.unlink(missing_ok=True)
        return InstallResult(False, paths,
                             "removed" if existed else "was not installed")
    return InstallResult(False, [], f"unsupported platform: {system}")


def notify(title: str, message: str, system: Optional[str] = None,
           run=subprocess.run) -> bool:
    """Desktop notification. Best-effort - never fail the check because of it."""
    system = system or platform.system()
    try:
        if system == "Darwin":
            body = message.replace('"', "'")[:300]
            run(["osascript", "-e",
                 f'display notification "{body}" with title "{title}"'],
                capture_output=True, text=True)
            return True
        if system == "Linux":
            run(["notify-send", title, message[:300]],
                capture_output=True, text=True)
            return True
    except (OSError, FileNotFoundError):
        return False
    return False
