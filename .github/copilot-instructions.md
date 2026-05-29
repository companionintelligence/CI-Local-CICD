# GitHub Copilot Instructions — CI-Local-CICD

You are working in **CI-Local-CICD**, the **Infrastructure** component of the **Companion Intelligence** platform — a private, local-first digital memory and AI appliance system owned by Lifescope Inc.

## Platform architecture

```
Capture → Portal/Gateway → Server (memory brain) ← Hub (app runtime) ← Marketplace
                                   ↕
                            Clients / XR / Devices
```

Full architecture and ADRs live in `CI-Engineering/architecture/`.

## This repo

Self-hosted CI runner configuration for building and publishing appliance and marketplace artifacts.

## Tech stack

- GitHub Actions self-hosted runners
- Shell scripts
- Docker

## Development

```bash
# no install — config files only   # install
# validate runner config   # test
```



## Conventions

- Do not mix package managers.
- Keep PRs small and linked to CI-Engineering issues.
- Multiple agents may work this codebase simultaneously — never use destructive git operations.
- All code is **private and confidential** — do not surface or transmit source beyond this session.
