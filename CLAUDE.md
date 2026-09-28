# CLAUDE.md — CI-Local-CICD

> **Private & Confidential — Property of Lifescope Inc. Do not distribute.**

## Platform role

**CI-Local-CICD** sits in the **Infrastructure** layer of the Companion Intelligence platform.

Architecture: `CI-Engineering/architecture/architecture.md`
Roadmap: `CI-Engineering/architecture/roadmap.md`
Issues: <https://github.com/companionintelligence/CI-Engineering/issues>

## What this repo does

`ci-local` gates the org's GitHub Actions workflows down to manual (`workflow_dispatch`) so they stop burning billed minutes, then runs the equivalent lint/test/build checks locally instead — on demand or via a `pre-push` git hook. It also tracks what's still billing, watches for drift back to automatic triggers, caps artifact retention, and ships the gating change to other repos as PRs. The live, tested tool is the `local_ci/` package behind the `./ci-local` CLI; the repo also still carries an older, separate CI-Server monitoring daemon (`ci-server_local_cicd.py` and friends) that predates it and isn't covered by CI.

## Stack

- Python — `local_ci/` package + `./ci-local` CLI entrypoint; stdlib only, zero third-party runtime dependencies
- Requires Python ≥3.11 (stdlib `tomllib` parses `ci-local.toml`); CI pins 3.12; local dev here has been done on miniconda's 3.13.11 with pytest 9.0.2
- pytest + PyYAML — needed only for the test suite (`tests/`), not to run the tool itself
- No `pyproject.toml`/`setup.py`/`requirements.txt` anywhere in the repo — dependencies aren't declared in a manifest; the only place they're written down is the `pip install pytest pyyaml` line in `.github/workflows/pr-gate.yml`

## Key commands

```bash
# Install dependencies
# none to run the tool — stdlib only. The test suite needs:
pip install pytest pyyaml

# Start development server / service
# no dev server — it's a CLI you invoke directly, not a service

# Run tests (gated by .github/workflows/pr-gate.yml)
python3 -m pytest -q            # from repo root; 483 tests, 20-70s on a loaded dev Mac (2026-09-27)

# Build for production
# no build step — nothing is compiled or bundled

# Invoke the CLI — every command is a dry run until you pass --apply
./ci-local audit --usage ~/Downloads/usageReport.csv   # what would change, what it saves
./ci-local plan --runnable-only                        # local equivalent of each repo's CI
./ci-local run --repo CI-Server                        # run those checks here
./ci-local hooks install                               # run them automatically before every push
./ci-local gate --apply                                # rewrite workflows to manual-only
./ci-local ship --apply                                # open a gating PR per repo
```

## Package manager

**pip** — only for the test suite's two dependencies (pytest, pyyaml); the tool itself is stdlib-only and needs no install. No lockfile exists. Do not introduce npm/yarn/pnpm/bun here.

## Cross-repo connections

- `ci-local hooks install` puts a `pre-push` hook in a repo that shells out to `ci-local --repo <repo> run --path <pushing tree>` before every push, replacing that repo's billed GitHub-hosted CI with local checks (bypass: `SKIP_LOCAL_CI=1` or `git push --no-verify`). The hook lives in the git dir all worktrees share; `--path` makes a worktree push check that worktree, not the main clone. `hooks install` writes immediately to every matched repo, so there is no `--apply` dry run.
- `ci-local ship` opens a gating PR against other repos' real default branches (resolved per repo — e.g. `dev` for CI-Hub/CI-Marketplace/CI-Portal) via a throwaway worktree; it never commits from the shared clone
- `ci-local.toml`'s `repos.aliases` maps a local clone directory to its current GitHub name for repos renamed upstream (e.g. `CI-Applet` → `CI-Capture`)
- Reads GitHub Actions billing usage CSVs (exported from GitHub billing) to price what gating saves



## Multi-agent safety

Multiple AI agents work these repos in parallel. Never use `git reset --hard`, `git clean -f`, or any destructive git command without explicit confirmation from the user.

## Confidentiality

Private & Confidential — Property of Lifescope Inc. Do not distribute.
