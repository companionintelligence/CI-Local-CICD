# AGENTS.md — CI-Local-CICD

## Platform context

CI-Local-CICD is the **Infrastructure** layer component of the Companion Intelligence platform — a private, local-first digital memory system.

Full architecture: `CI-Engineering/architecture/architecture.md`

## Repo summary

Self-hosted CI runner configuration for building and publishing appliance and marketplace artifacts.

## Stack

- GitHub Actions self-hosted runners
- Shell scripts
- Docker

## Setup & commands

```bash
# no install — config files only   # install
# no dev server   # dev
# validate runner config   # test
# deploy runner config   # build
```



## Git discipline

- Never `git reset --hard` or `git clean -f` without user confirmation.
- Commit messages: imperative mood, ≤ 72 chars.
- Reference CI-Engineering issues in PRs (e.g. `Closes companionintelligence/CI-Engineering#32`).

## Confidentiality

Private & Confidential — Property of Lifescope Inc. Do not distribute.
