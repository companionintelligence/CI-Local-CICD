#!/usr/bin/env python3
"""
A simple Python daemon that checks a Git repository's main branch every 5 minutes,
pulls updates when detected, and runs setup commands.

Configuration:
  - Set REPO_DIR to the local clone path
  - Adjust GIT_REMOTE and GIT_BRANCH as needed
  - Update SETUP_COMMANDS with the shell commands to run post-pull

Usage:
  $ git checkout https://github.com/companionintelligence/CI-Server
  $ chmod +x git_update_daemon.py
  $ ./ci-server_local_cicd.py &
"""
import os
import time
import subprocess
import logging

# Configuration
REPO_DIR = os.path.expanduser("~/devel/CI-Server")      # Local repository path
GIT_REMOTE = "origin"
GIT_BRANCH = "main"
CHECK_INTERVAL = 5 * 60  # 5 minutes in seconds

# Commands to run after pulling updates
SETUP_COMMANDS = [
    "pip install -r requirements.txt",
    "./setup.sh",
    # add more commands as needed
]

# Logging setup
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )


def run_cmd(cmd, cwd=None, shell=False):
    """Run a subprocess command and return stdout as text."""
    logging.info(f"Running command: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    result = subprocess.run(
        cmd,
        cwd=cwd,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    if result.returncode != 0:
        logging.error(f"Command failed: {result.stdout.strip()}")
    else:
        logging.info(f"Command output: {result.stdout.strip()}")
    return result


def get_local_revision():
    """Get the current local HEAD revision."""
    res = run_cmd(["git", "rev-parse", "HEAD"], cwd=REPO_DIR)
    return res.stdout.strip()


def get_remote_revision():
    """Fetch remote and get the remote branch revision."""
    run_cmd(["git", "fetch", GIT_REMOTE, GIT_BRANCH], cwd=REPO_DIR)
    res = run_cmd(["git", "rev-parse", f"{GIT_REMOTE}/{GIT_BRANCH}"], cwd=REPO_DIR)
    return res.stdout.strip()


def pull_updates():
    """Pull updates from the remote branch."""
    run_cmd(["git", "pull", GIT_REMOTE, GIT_BRANCH], cwd=REPO_DIR)


def run_setup_commands():
    """Execute configured setup shell commands."""
    for cmd in SETUP_COMMANDS:
        run_cmd(cmd, cwd=REPO_DIR, shell=True)


def main():
    setup_logging()
    logging.info("Starting Git update daemon...")

    # Ensure repo directory exists
    if not os.path.isdir(REPO_DIR):
        logging.error(f"Repository directory not found: {REPO_DIR}")
        return

    last_revision = get_local_revision()
    logging.info(f"Current revision: {last_revision}")

    while True:
        try:
            remote_revision = get_remote_revision()
            if remote_revision != last_revision:
                logging.info(f"New update detected: {last_revision} -> {remote_revision}")
                pull_updates()
                run_setup_commands()
                last_revision = get_local_revision()
            else:
                logging.debug("No updates found.")
        except Exception as e:
            logging.exception(f"Error checking for updates: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main()
