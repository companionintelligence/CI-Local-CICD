# Local CI CD Tooling

## COPYRIGHT COMPANION INTELLIGENCE - LIFESCOPE INC

## DO NOT DISTRIBUTE

## PRIVATE AND CONFIDENTIAL
[CloudFlare ZeroTrust Proxy Tutorial](https://docs.google.com/document/d/1zsFws3DiKwXNGd5hF0prV1OFkR6H7oNLQubNlucqETo/edit?tab=t.0)


A simple Python daemon that checks a Git repository's main branch every 5 minutes,
pulls updates when detected, and runs setup commands.

Configuration:
  - Set REPO_DIR to the local clone path
  - Adjust GIT_REMOTE and GIT_BRANCH as needed
  - Update SETUP_COMMANDS with the shell commands to run post-pull

Usage:
```
git checkout https://github.com/companionintelligence/CI-Server
chmod +x git_update_daemon.py
./ci-server_local_cicd.py &
```
