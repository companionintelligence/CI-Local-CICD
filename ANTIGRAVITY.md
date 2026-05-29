# ANTIGRAVITY.md — CI-Local-CICD

## Context

**CI-Local-CICD** — **Infrastructure** layer of the Companion Intelligence platform (Lifescope Inc).

Architecture: `CI-Engineering/architecture/architecture.md`
Roadmap: `CI-Engineering/architecture/roadmap.md`

## Purpose

Self-hosted CI runner configuration for building and publishing appliance and marketplace artifacts.

## Stack

- GitHub Actions self-hosted runners
- Shell scripts
- Docker

## Commands

```bash
# no install — config files only
# no dev server
# validate runner config
```

## Rules

- Package manager: do not switch or mix.
- Destructive git operations (`reset --hard`, `clean -f`) require explicit user confirmation.
- Commits reference CI-Engineering issues.
- All content is **private and confidential** — Property of Lifescope Inc.
