# CLAUDE.md — CI-Local-CICD

> **Private & Confidential — Property of Lifescope Inc. Do not distribute.**

## Platform role

**CI-Local-CICD** sits in the **Infrastructure** layer of the Companion Intelligence platform.

Architecture: `CI-Engineering/architecture/architecture.md`
Roadmap: `CI-Engineering/architecture/roadmap.md`
Issues: <https://github.com/companionintelligence/CI-Engineering/issues>

## What this repo does

Self-hosted CI runner configuration for building and publishing appliance and marketplace artifacts.

## Stack

- GitHub Actions self-hosted runners
- Shell scripts
- Docker

## Key commands

```bash
# Install dependencies
# no install — config files only

# Start development server / service
# no dev server

# Run tests
# validate runner config

# Build for production
# deploy runner config
```

## Package manager

**bash** — use exclusively; do not mix with npm/yarn/pnpm/bun unless the repo explicitly requires it.

## Cross-repo connections

- Publishes CI-Marketplace app images to GHCR
- Builds CI-OS images
- Interacts with CI-Portal entitlement system



## Multi-agent safety

Multiple AI agents work these repos in parallel. Never use `git reset --hard`, `git clean -f`, or any destructive git command without explicit confirmation from the user.

## Confidentiality

Private & Confidential — Property of Lifescope Inc. Do not distribute.
