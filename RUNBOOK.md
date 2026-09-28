# Running CI by hand

Automatic workflow triggers are gated: pushing no longer starts a GitHub-hosted
run. Checks run on local hardware, and the workflows that remain are started
deliberately.

This file is generated - run `ci-local docs` to refresh it. Do not hand-edit.

## The short version

```bash
# Run this repo's checks locally, the way CI used to
ci-local run --repo <REPO>

# Run everything that changed across the workspace
ci-local run --all

# Install the pre-push hook so you cannot forget (dry run, then apply)
ci-local hooks install
ci-local hooks install --apply
```

To start a gated workflow on GitHub you need the `gh` CLI and push access:

```bash
gh workflow run <file>.yml --repo companionintelligence/<REPO> --ref <branch>
```

`--ref` matters: the workflow file is read from that branch, so a workflow that
only exists on your feature branch must be dispatched against it.

Watch it with `gh run watch --repo companionintelligence/<REPO>`.

---

## .github

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## .github-private

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## AD4M-Container

Default branch: ``

_Could not read workflows: cannot resolve default branch_

---

## cache-mounts

Default branch: `master`

### Test locally

_No local checks detected._

### Cut a release

**Publish container** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/cache-mounts --ref master
```

---

## CI-Active-Inference

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-Brand-Studio

Default branch: `main`

### Test locally

```bash
ci-local run --repo CI-Brand-Studio
```

Equivalent to:

```bash
pnpm install --frozen-lockfile
pnpm run lint
pnpm run typecheck
pnpm run test
pnpm run build
```

### Still automatic

These run without being asked - no action needed:

- `ci.yml` on push, pull_request

---

## CI-Browser-Extension

Default branch: `master`

### Test locally

```bash
ci-local run --repo CI-Browser-Extension
```

Equivalent to:

```bash
yarn install --immutable
yarn run lint
yarn run test
yarn run build
```

### Run a check workflow on GitHub

**Build and Test** — `build-and-test.yml`

```bash
gh workflow run build-and-test.yml --repo companionintelligence/CI-Browser-Extension --ref master
```

**Style canon lint** — `style-canon-lint.yml`

```bash
gh workflow run style-canon-lint.yml --repo companionintelligence/CI-Browser-Extension --ref master
```

**Verify Store Credentials** — `verify-store-credentials.yml`

```bash
gh workflow run verify-store-credentials.yml --repo companionintelligence/CI-Browser-Extension --ref master
```

### Still automatic

These run without being asked - no action needed:

- `release.yml` on push

---

## CI-Capture

Default branch: `main`

### Test locally

```bash
ci-local run --repo CI-Capture
```

Equivalent to:

```bash
cargo fmt --all --check   # optional
cargo clippy --all-targets -- -D warnings
cargo test --all
cargo build --release   # optional
```

### Run a check workflow on GitHub

**CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/CI-Capture --ref main
```

**Packages** — `packages.yml`

```bash
gh workflow run packages.yml --repo companionintelligence/CI-Capture --ref main
```

### Still automatic

These run without being asked - no action needed:

- `release.yml` on push

---

## CI-Common

Default branch: `main`

### Test locally

_No local checks detected._

### Still automatic

These run without being asked - no action needed:

- `frontend-tests.yml` on push, pull_request
- `publish-icons.yml` on push
- `publish-marketing.yml` on release
- `publish-tokens.yml` on release
- `publish-ui.yml` on push
- `style-canon-lint.yml` on push, pull_request

---

## CI-Demo-Companions-John-Smith

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-Docs

Default branch: `main`

### Test locally

```bash
ci-local run --repo CI-Docs
```

Equivalent to:

```bash
bun install --frozen-lockfile
bun run lint
bun run build
```

### Run a check workflow on GitHub

**Style canon lint** — `style-canon-lint.yml`

```bash
gh workflow run style-canon-lint.yml --repo companionintelligence/CI-Docs --ref main
```

### Still automatic

These run without being asked - no action needed:

- `deploy-cloudflare.yml` on push, pull_request

---

## CI-Earth-2

Default branch: `main`

### Test locally

```bash
ci-local run --repo CI-Earth-2
```

Equivalent to:

```bash
npm ci
npm run --silent lint
npm run --silent test
npm run --silent build
```

### Cut a release

**Publish container** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/CI-Earth-2 --ref main
```

---

## CI-Engineering

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-Even-Realities

Default branch: `main`

### Test locally

_No local checks detected._

### Run a check workflow on GitHub

**CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/CI-Even-Realities --ref main
```

**Style canon lint** — `style-canon-lint.yml`

```bash
gh workflow run style-canon-lint.yml --repo companionintelligence/CI-Even-Realities --ref main
```

---

## CI-GonoPBX

Default branch: `main`

### Test locally

_No local checks detected._

### Run a check workflow on GitHub

**Build & publish GonoPBX images** — `build-images.yml`

```bash
gh workflow run build-images.yml --repo companionintelligence/CI-GonoPBX --ref main -f gonopbx_ref=v2.1.1
```

Required inputs: `gonopbx_ref` (Upstream ankaios76/gonopbx git ref (tag/branch/sha) to build)

---

## CI-Hermes

Default branch: `master`

### Test locally

```bash
ci-local run --repo CI-Hermes
```

Equivalent to:

```bash
npm ci
npm run --silent test:unit
```

### Cut a release

**Docker Build and Push** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/CI-Hermes --ref master
```

**CI-Hermes Release PR** — `hermes-release-pr.yml`

```bash
gh workflow run hermes-release-pr.yml --repo companionintelligence/CI-Hermes --ref master
```

**Tag CI-Hermes Version On Merge** — `tag-on-hermes-merge.yml`

```bash
gh workflow run tag-on-hermes-merge.yml --repo companionintelligence/CI-Hermes --ref master
```

### Still automatic

These run without being asked - no action needed:

- `ci.yml` on pull_request, push
- `npm-publish.yml` on workflow_run, push

---

## CI-Home-Assistant-Voice-PE

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-Hub

Default branch: `dev`

### Test locally

```bash
ci-local run --repo CI-Hub
```

Equivalent to:

```bash
pnpm install --frozen-lockfile
pnpm run lint
pnpm run tsc
pnpm run test
pnpm run build
```

### Run a check workflow on GitHub

**Agent Gates** — `agent-gates.yml`

```bash
gh workflow run agent-gates.yml --repo companionintelligence/CI-Hub --ref dev
```

**App Catalog Fleet Tests** — `app-catalog-fleet.yml`

```bash
gh workflow run app-catalog-fleet.yml --repo companionintelligence/CI-Hub --ref dev -f mode=full -f batch=0 -f server=core-1
```

Required inputs: `mode` (Test mode)

**Build and Push Container** — `build-container.yml`

```bash
gh workflow run build-container.yml --repo companionintelligence/CI-Hub --ref dev -f environment=<choice> -f tag=<string>
```

Required inputs: `environment` (Environment to deploy to)

**Hub CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/CI-Hub --ref dev
```

**CodeQL - Code Quality** — `codeql.yml`

```bash
gh workflow run codeql.yml --repo companionintelligence/CI-Hub --ref dev
```

**Dependency Review** — `dependency-review.yml`

```bash
gh workflow run dependency-review.yml --repo companionintelligence/CI-Hub --ref dev
```

**Desktop Build** — `desktop-build.yml`

```bash
gh workflow run desktop-build.yml --repo companionintelligence/CI-Hub --ref dev
```

**Desktop Tests** — `desktop-tests.yml`

```bash
gh workflow run desktop-tests.yml --repo companionintelligence/CI-Hub --ref dev
```

**E2E Extended** — `e2e-extended.yml`

```bash
gh workflow run e2e-extended.yml --repo companionintelligence/CI-Hub --ref dev
```

**Fleet E2E Tests** — `e2e-fleet.yml`

```bash
gh workflow run e2e-fleet.yml --repo companionintelligence/CI-Hub --ref dev -f server=core-6 -f test_suite=all
```

Required inputs: `server` (Target server), `test_suite` (Test suite to run)

**E2E MCP** — `e2e-mcp.yml`

```bash
gh workflow run e2e-mcp.yml --repo companionintelligence/CI-Hub --ref dev
```

**E2E Platform Tests** — `e2e-platform.yml`

```bash
gh workflow run e2e-platform.yml --repo companionintelligence/CI-Hub --ref dev
```

**E2E Tests** — `e2e.yml`

```bash
gh workflow run e2e.yml --repo companionintelligence/CI-Hub --ref dev
```

**Integraton tests** — `integration-tests.yml`

```bash
gh workflow run integration-tests.yml --repo companionintelligence/CI-Hub --ref dev
```

**PR Compliance** — `pr-compliance.yml`

```bash
gh workflow run pr-compliance.yml --repo companionintelligence/CI-Hub --ref dev
```

**Secret Scan** — `secret-scan.yml`

```bash
gh workflow run secret-scan.yml --repo companionintelligence/CI-Hub --ref dev
```

### Cut a release

**Desktop Release** — `desktop-release.yml`

```bash
gh workflow run desktop-release.yml --repo companionintelligence/CI-Hub --ref dev -f tag=<string> -f prerelease=true -f environment=dev -f platforms=all
```

Required inputs: `tag` (Release tag (e.g. v0.2.27))

**Nightly Release** — `nightly-release.yml`

```bash
gh workflow run nightly-release.yml --repo companionintelligence/CI-Hub --ref dev
```

**Publish package managers** — `publish-package-managers.yml`

```bash
gh workflow run publish-package-managers.yml --repo companionintelligence/CI-Hub --ref dev -f tag=<string>
```

Required inputs: `tag` (Release tag (e.g. v0.2.28))

**Auto Semver Tag** — `semver-tag.yml`

```bash
gh workflow run semver-tag.yml --repo companionintelligence/CI-Hub --ref dev -f bump=<string>
```

---

## CI-Import-Tools

Default branch: `main`

### Test locally

```bash
ci-local run --repo CI-Import-Tools
```

Equivalent to:

```bash
npm ci
npm run --silent lint
npm run --silent test
npm run --silent build
```

### Run a check workflow on GitHub

**CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/CI-Import-Tools --ref main
```

**Style canon lint** — `style-canon-lint.yml`

```bash
gh workflow run style-canon-lint.yml --repo companionintelligence/CI-Import-Tools --ref main
```

### Cut a release

**Publish container** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/CI-Import-Tools --ref main
```

---

## CI-Local-CICD

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-MaixCAM

Default branch: `main`

### Test locally

_No local checks detected._

### Run a check workflow on GitHub

**Build MaixCAM** — `build_maixcam.yml`

```bash
gh workflow run build_maixcam.yml --repo companionintelligence/CI-MaixCAM --ref main
```

**Build MaixCAM Dev branch** — `build_maixcam_dev.yml`

```bash
gh workflow run build_maixcam_dev.yml --repo companionintelligence/CI-MaixCAM --ref main
```

**Build MaixCAM PR** — `build_maixcam_main_pr.yml`

```bash
gh workflow run build_maixcam_main_pr.yml --repo companionintelligence/CI-MaixCAM --ref main
```

**sync code to gitee** — `sync_gitee.yml`

```bash
gh workflow run sync_gitee.yml --repo companionintelligence/CI-MaixCAM --ref main
```

**trigger wiki** — `trigger_wiki.yml`

```bash
gh workflow run trigger_wiki.yml --repo companionintelligence/CI-MaixCAM --ref main
```

### Cut a release

**Relase MaixPy for MaixCAM** — `release_maixcam.yml`

```bash
gh workflow run release_maixcam.yml --repo companionintelligence/CI-MaixCAM --ref main
```

**Relase MaixPy for Linux** — `release_other_linux.yml`

```bash
gh workflow run release_other_linux.yml --repo companionintelligence/CI-MaixCAM --ref main
```

---

## CI-Marketing

Default branch: `main`

### Test locally

```bash
ci-local run --repo CI-Marketing
```

Equivalent to:

```bash
npm ci
npm run --silent lint
npm run --silent test
npm run --silent build
```

### Run a check workflow on GitHub

**studio-ci** — `studio-ci.yml`

```bash
gh workflow run studio-ci.yml --repo companionintelligence/CI-Marketing --ref main
```

---

## CI-Marketplace

Default branch: `dev`

### Test locally

```bash
ci-local run --repo CI-Marketplace
```

Equivalent to:

```bash
pnpm install --frozen-lockfile
pnpm run test
```

### Run a check workflow on GitHub

**App Onboarding** — `app-onboard.yml`

```bash
gh workflow run app-onboard.yml --repo companionintelligence/CI-Marketplace --ref dev -f slug=<value> -f upstream=<value> -f image=<value> -f category=<choice> -f alternative=<value> -f notes=<value> -f logo_url=<value> -f description=<value> -f issue=<value>
```

Required inputs: `slug` (App slug (e.g. jellyfin))

**App Update** — `app-update.yml`

```bash
gh workflow run app-update.yml --repo companionintelligence/CI-Marketplace --ref dev -f only=<value> -f dry_run=false -f skip_verify=false
```

**Build AI Job Search** — `build-ai-job-search.yml`

```bash
gh workflow run build-ai-job-search.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build ASCIIFlow** — `build-asciiflow.yml`

```bash
gh workflow run build-asciiflow.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build Beatsync** — `build-beatsync.yml`

```bash
gh workflow run build-beatsync.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build Blockbench** — `build-blockbench.yml`

```bash
gh workflow run build-blockbench.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build CosyVoice** — `build-cosyvoice.yml`

```bash
gh workflow run build-cosyvoice.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build FreeCut** — `build-freecut.yml`

```bash
gh workflow run build-freecut.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build Gyroflow** — `build-gyroflow.yml`

```bash
gh workflow run build-gyroflow.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build LLMFit** — `build-llmfit.yml`

```bash
gh workflow run build-llmfit.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build Louter** — `build-louter.yml`

```bash
gh workflow run build-louter.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build OpenCut** — `build-opencut.yml`

```bash
gh workflow run build-opencut.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build OpenShell wrappers** — `build-openshell-wrappers.yml`

```bash
gh workflow run build-openshell-wrappers.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build Remotion Studio** — `build-remotion-studio.yml`

```bash
gh workflow run build-remotion-studio.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build SafeOS API mirror** — `build-safeos-api.yml`

```bash
gh workflow run build-safeos-api.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build Second Me** — `build-secondme.yml`

```bash
gh workflow run build-secondme.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build Torollo** — `build-torollo.yml`

```bash
gh workflow run build-torollo.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**Build World Monitor** — `build-worldmonitor.yml`

```bash
gh workflow run build-worldmonitor.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/CI-Marketplace --ref dev
```

**PR Compliance** — `pr-compliance.yml`

```bash
gh workflow run pr-compliance.yml --repo companionintelligence/CI-Marketplace --ref dev
```

### Still automatic

These run without being asked - no action needed:

- `deploy.yml` on push

---

## CI-Marketplace-Requests

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-Mentra

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-MetaHuman

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-OMI

Default branch: `main`

### Test locally

_No local checks detected._

### Run a check workflow on GitHub

**Entelligence-AI** — `entellegence_issues.yml`

```bash
gh workflow run entellegence_issues.yml --repo companionintelligence/CI-OMI --ref main
```

**Code Review** — `entelligence-pr-reviewer.yml`

```bash
gh workflow run entelligence-pr-reviewer.yml --repo companionintelligence/CI-OMI --ref main
```

**Deploy Apps-Js to Cloud RUN** — `gcp_apps_js.yml`

```bash
gh workflow run gcp_apps_js.yml --repo companionintelligence/CI-OMI --ref main -f environment=development -f branch=main
```

Required inputs: `environment` (Select the environment to deploy to), `branch` (Branch to deploy from)

**Deploy Backend to Cloud RUN** — `gcp_backend.yml`

```bash
gh workflow run gcp_backend.yml --repo companionintelligence/CI-OMI --ref main -f environment=development -f branch=main
```

Required inputs: `environment` (Select the environment to deploy to), `branch` (Branch to deploy from)

**Deploy Backend Pusher to Cloud RUN** — `gcp_backend_pusher.yml`

```bash
gh workflow run gcp_backend_pusher.yml --repo companionintelligence/CI-OMI --ref main -f environment=development -f branch=main
```

Required inputs: `environment` (Select the environment to deploy to), `branch` (Branch to deploy from)

**Deploy Frontend to Cloud RUN** — `gcp_frontend.yml`

```bash
gh workflow run gcp_frontend.yml --repo companionintelligence/CI-OMI --ref main -f branch=main
```

Required inputs: `branch` (Branch to deploy from)

**Deploy Speech Models to Cloud RUN** — `gcp_models.yml`

```bash
gh workflow run gcp_models.yml --repo companionintelligence/CI-OMI --ref main -f environment=development -f branch=main
```

Required inputs: `environment` (Select the environment to deploy to), `branch` (Branch to deploy from)

**Deploy Notifications Job to Cloud RUN** — `gcp_notifications_job.yml`

```bash
gh workflow run gcp_notifications_job.yml --repo companionintelligence/CI-OMI --ref main -f environment=development -f branch=main
```

Required inputs: `environment` (Select the environment to deploy to), `branch` (Branch to deploy from)

**Deploy Personas to Cloud RUN** — `gcp_personas.yml`

```bash
gh workflow run gcp_personas.yml --repo companionintelligence/CI-OMI --ref main -f branch=main
```

Required inputs: `branch` (Branch to deploy from)

**Deploy Plugins to Cloud RUN** — `gcp_plugins.yml`

```bash
gh workflow run gcp_plugins.yml --repo companionintelligence/CI-OMI --ref main
```

**GitHub Issue Sync** — `main.yml`

```bash
gh workflow run main.yml --repo companionintelligence/CI-OMI --ref main -f excludeClosed=true
```

**Sync Mintlify Docs back to Github** — `sync-docs.yml`

```bash
gh workflow run sync-docs.yml --repo companionintelligence/CI-OMI --ref main
```

### Cut a release

**Deploy static content to Pages** — `deploy_docs.yml`

```bash
gh workflow run deploy_docs.yml --repo companionintelligence/CI-OMI --ref main
```

---

## CI-OpenClaw

Default branch: `master`

### Test locally

```bash
ci-local run --repo CI-OpenClaw
```

Equivalent to:

```bash
npm ci
npm run --silent test:unit
```

### Run a check workflow on GitHub

**ci-hub plugin bundle resync** — `ci-hub-plugin-resync.yml`

```bash
gh workflow run ci-hub-plugin-resync.yml --repo companionintelligence/CI-OpenClaw --ref master -f ref=dev
```

**CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/CI-OpenClaw --ref master
```

### Cut a release

**Docker Build and Push** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/CI-OpenClaw --ref master -f tag=<string> -f dry_run=false
```

Required inputs: `tag` (Existing release tag to (re)publish (for example 2026.6.1))

**Publish memory plugin to npm** — `npm-publish.yml`

```bash
gh workflow run npm-publish.yml --repo companionintelligence/CI-OpenClaw --ref master -f tag=<string> -f dry_run=false
```

Required inputs: `tag` (Existing release tag to publish (for example 2026.8.1))

**CI-OpenClaw Release PR** — `openclaw-release-pr.yml`

```bash
gh workflow run openclaw-release-pr.yml --repo companionintelligence/CI-OpenClaw --ref master
```

---

## CI-Operations

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-OS

Default branch: `main`

### Test locally

_No local checks detected._

### Run a check workflow on GitHub

**Build Companion OS ISO** — `build-companion-os-iso.yml`

```bash
gh workflow run build-companion-os-iso.yml --repo companionintelligence/CI-OS --ref main -f version=1.0 -f sign=false
```

Required inputs: `version` (CI-OS version (e.g., 1.0, 1.1))

**Style canon lint** — `style-canon-lint.yml`

```bash
gh workflow run style-canon-lint.yml --repo companionintelligence/CI-OS --ref main
```

---

## CI-Outreach

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-Pebble

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-Photo-Time-Machine

Default branch: `main`

### Test locally

_No local checks detected._

### Run a check workflow on GitHub

**CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/CI-Photo-Time-Machine --ref main
```

---

## CI-Photo-Time-Machine-2

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-Planning

Default branch: `main`

### Test locally

```bash
ci-local run --repo CI-Planning
```

Equivalent to:

```bash
pnpm install --frozen-lockfile
pnpm run typecheck
pnpm run test
pnpm run build
```

### Run a check workflow on GitHub

**CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/CI-Planning --ref main
```

**Style canon lint** — `style-canon-lint.yml`

```bash
gh workflow run style-canon-lint.yml --repo companionintelligence/CI-Planning --ref main
```

### Cut a release

**Publish container** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/CI-Planning --ref main
```

---

## CI-Portal

Default branch: `dev`

### Test locally

```bash
ci-local run --repo CI-Portal
```

Equivalent to:

```bash
pnpm install --frozen-lockfile
pnpm run lint
pnpm run test
pnpm run build
```

### Run a check workflow on GitHub

**CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/CI-Portal --ref dev
```

**Dependency Review** — `dependency-review.yml`

```bash
gh workflow run dependency-review.yml --repo companionintelligence/CI-Portal --ref dev
```

**Playwright - E2E Tests** — `playwright.yml`

```bash
gh workflow run playwright.yml --repo companionintelligence/CI-Portal --ref dev
```

**Secret Scan** — `secret-scan.yml`

```bash
gh workflow run secret-scan.yml --repo companionintelligence/CI-Portal --ref dev
```

**Style canon lint** — `style-canon-lint.yml`

```bash
gh workflow run style-canon-lint.yml --repo companionintelligence/CI-Portal --ref dev
```

### Still automatic

These run without being asked - no action needed:

- `deploy.yml` on push

---

## CI-Public-Website

Default branch: `main`

### Test locally

```bash
ci-local run --repo CI-Public-Website
```

Equivalent to:

```bash
npm ci
npm run --silent build
```

### Run a check workflow on GitHub

**Build snippets** — `build.yml`

```bash
gh workflow run build.yml --repo companionintelligence/CI-Public-Website --ref main
```

**Style canon lint** — `style-canon-lint.yml`

```bash
gh workflow run style-canon-lint.yml --repo companionintelligence/CI-Public-Website --ref main
```

### Cut a release

**Deploy site (Cloudflare Pages)** — `deploy-site.yml`

```bash
gh workflow run deploy-site.yml --repo companionintelligence/CI-Public-Website --ref main
```

---

## CI-Server

Default branch: `main`

### Test locally

```bash
ci-local run --repo CI-Server
```

Equivalent to:

```bash
yarn install --immutable
yarn run lint
```

### Run a check workflow on GitHub

**CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/CI-Server --ref main
```

**E2E** — `e2e.yml`

```bash
gh workflow run e2e.yml --repo companionintelligence/CI-Server --ref main
```

**Migrations** — `migration-integrity-check.yml`

```bash
gh workflow run migration-integrity-check.yml --repo companionintelligence/CI-Server --ref main
```

**Style canon lint** — `style-canon-lint.yml`

```bash
gh workflow run style-canon-lint.yml --repo companionintelligence/CI-Server --ref main
```

### Cut a release

**Publish** — `publish.yml`

```bash
gh workflow run publish.yml --repo companionintelligence/CI-Server --ref main
```

---

## CI-Spatial-Companion-Unity

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-Spatial-Companion-Web

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-Spatial-Companion-WebXR

Default branch: `main`

### Test locally

```bash
ci-local run --repo CI-Spatial-Companion-WebXR
```

Equivalent to:

```bash
pnpm install --frozen-lockfile
pnpm run lint
pnpm run test
pnpm run build
```

### Run a check workflow on GitHub

**Style canon lint** — `style-canon-lint.yml`

```bash
gh workflow run style-canon-lint.yml --repo companionintelligence/CI-Spatial-Companion-WebXR --ref main
```

**Test** — `test.yml`

```bash
gh workflow run test.yml --repo companionintelligence/CI-Spatial-Companion-WebXR --ref main
```

### Cut a release

**Publish container** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/CI-Spatial-Companion-WebXR --ref main
```

**Publish proxy container** — `publish-proxy.yml`

```bash
gh workflow run publish-proxy.yml --repo companionintelligence/CI-Spatial-Companion-WebXR --ref main
```

### Still automatic

These run without being asked - no action needed:

- `pages-deploy.yml` on push

---

## CI-Spellbook

Default branch: `main`

### Test locally

```bash
ci-local run --repo CI-Spellbook
```

Equivalent to:

```bash
npm ci
npm run --silent test
npm run --silent build
```

### Run a check workflow on GitHub

**CI and Deploy** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/CI-Spellbook --ref main
```

### Cut a release

**Publish container** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/CI-Spellbook --ref main
```

---

## CI-Splatviewer-VR-TESTBED

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## CI-Static-Containter-Builder

Default branch: `main`

### Test locally

_No local checks detected._

### Cut a release

**Publish container** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/CI-Static-Containter-Builder --ref main
```

---

## CI-Test-Data-Person-1-John-Smith

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## ci-time-machine

Default branch: `feat/m1-m2-initial-monorepo`

### Test locally

```bash
ci-local run --repo ci-time-machine
```

Equivalent to:

```bash
pnpm install --frozen-lockfile
pnpm run lint
pnpm run typecheck
pnpm run test
pnpm run build
```

### Cut a release

**Publish container** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/ci-time-machine --ref feat/m1-m2-initial-monorepo
```

---

## CI-Tools-Cache-Mounts

Default branch: `master`

### Test locally

_No local checks detected._

### Cut a release

**Publish container** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/CI-Tools-Cache-Mounts --ref master
```

---

## CI-Web-XR-Scan

Default branch: `main`

### Test locally

```bash
ci-local run --repo CI-Web-XR-Scan
```

Equivalent to:

```bash
npm ci
npm run --silent test
npm run --silent build
```

### Cut a release

**Publish container** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/CI-Web-XR-Scan --ref main
```

---

## CI-WebXR-Scan-Meta

Default branch: `main`

### Test locally

_No local checks detected._

### Run a check workflow on GitHub

**CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/CI-WebXR-Scan-Meta --ref main
```

---

## CI-WebXR-Time-Machine

Default branch: `main`

### Test locally

```bash
ci-local run --repo CI-WebXR-Time-Machine
```

Equivalent to:

```bash
npm ci
npm run --silent lint
npm run --silent typecheck
npm run --silent test
npm run --silent build
```

### Run a check workflow on GitHub

**CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/CI-WebXR-Time-Machine --ref main
```

### Cut a release

**Deploy Cloudflare Pages Preview** — `deploy-cloudflare-pages-preview.yml`

```bash
gh workflow run deploy-cloudflare-pages-preview.yml --repo companionintelligence/CI-WebXR-Time-Machine --ref main
```

**Publish container** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/CI-WebXR-Time-Machine --ref main
```

### Still automatic

These run without being asked - no action needed:

- `deploy-cloudflare-pages.yml` on push

---

## CI-XR-Scan-Web-Unity

Default branch: `main`

### Test locally

_No local checks detected._

### Run a check workflow on GitHub

**Unity Tests** — `unity-tests.yml`

```bash
gh workflow run unity-tests.yml --repo companionintelligence/CI-XR-Scan-Web-Unity --ref main
```

**Web Tests** — `web-tests.yml`

```bash
gh workflow run web-tests.yml --repo companionintelligence/CI-XR-Scan-Web-Unity --ref main
```

---

## comfy

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## Companion-Atlas

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## Companion-Pipe

Default branch: `main`

### Test locally

```bash
ci-local run --repo Companion-Pipe
```

Equivalent to:

```bash
cargo fmt --all --check   # optional
cargo clippy --all-targets -- -D warnings
cargo test --all
cargo build --release   # optional
```

### Run a check workflow on GitHub

**Rust Benchmark** — `benchmark.yml`

```bash
gh workflow run benchmark.yml --repo companionintelligence/Companion-Pipe --ref main
```

**Rust CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/Companion-Pipe --ref main
```

**Docs** — `docs.yml`

```bash
gh workflow run docs.yml --repo companionintelligence/Companion-Pipe --ref main
```

**macOS E2E Tests** — `e2e-macos.yml`

```bash
gh workflow run e2e-macos.yml --repo companionintelligence/Companion-Pipe --ref main
```

**E2E Tests** — `e2e-test.yml`

```bash
gh workflow run e2e-test.yml --repo companionintelligence/Companion-Pipe --ref main
```

**Audio Eval** — `eval-diarization.yml`

```bash
gh workflow run eval-diarization.yml --repo companionintelligence/Companion-Pipe --ref main -f fixtures=abjxc,bxpwa,dhorc
```

**Meeting Detection Eval** — `eval-meeting-detection.yml`

```bash
gh workflow run eval-meeting-detection.yml --repo companionintelligence/Companion-Pipe --ref main
```

**Secret Scan (trufflehog)** — `gitleaks.yml`

```bash
gh workflow run gitleaks.yml --repo companionintelligence/Companion-Pipe --ref main
```

**SDK** — `sdk.yml`

```bash
gh workflow run sdk.yml --repo companionintelligence/Companion-Pipe --ref main
```

**Sentry Cleanup - Resolve Stale Issues** — `sentry-cleanup.yml`

```bash
gh workflow run sentry-cleanup.yml --repo companionintelligence/Companion-Pipe --ref main -f version_gap=5 -f dry_run=false
```

**Style canon lint** — `style-canon-lint.yml`

```bash
gh workflow run style-canon-lint.yml --repo companionintelligence/Companion-Pipe --ref main
```

**Code Quality & Optimization** — `style.yml`

```bash
gh workflow run style.yml --repo companionintelligence/Companion-Pipe --ref main
```

**sync-skills** — `sync-skills.yml`

```bash
gh workflow run sync-skills.yml --repo companionintelligence/Companion-Pipe --ref main
```

**Test CLI npm (blank slate)** — `test-cli-npm.yml`

```bash
gh workflow run test-cli-npm.yml --repo companionintelligence/Companion-Pipe --ref main
```

**Frontend Tests** — `test-frontend.yml`

```bash
gh workflow run test-frontend.yml --repo companionintelligence/Companion-Pipe --ref main
```

**Test Code Signing** — `test-signing.yml`

```bash
gh workflow run test-signing.yml --repo companionintelligence/Companion-Pipe --ref main
```

**Windows CLI Integration Test** — `windows-integration-test.yml`

```bash
gh workflow run windows-integration-test.yml --repo companionintelligence/Companion-Pipe --ref main
```

### Cut a release

**privacy-filter release** — `privacy-filter-release.yml`

```bash
gh workflow run privacy-filter-release.yml --repo companionintelligence/Companion-Pipe --ref main
```

**Release Browser Extension** — `release-browser-extension.yml`

```bash
gh workflow run release-browser-extension.yml --repo companionintelligence/Companion-Pipe --ref main -f auto_publish=true
```

**Release Enterprise** — `release-enterprise.yml`

```bash
gh workflow run release-enterprise.yml --repo companionintelligence/Companion-Pipe --ref main
```

**Release SDK** — `sdk-release.yml`

```bash
gh workflow run sdk-release.yml --repo companionintelligence/Companion-Pipe --ref main -f version=0.1.0 -f npm_tag=latest -f publish_npm=false -f confirm=dry-run
```

Required inputs: `version` (SDK semver version already committed in ee/sdk/package.json), `npm_tag` (npm dist-tag to publish under), `publish_npm` (Publish public npm packages instead of dry-run only), `confirm` (Required when publishing: publish-sdk-VERSION)

### Still automatic

These run without being asked - no action needed:

- `release-app.yml` on push
- `release-cli.yml` on push
- `release-mcp.yml` on push

---

## CompanionHub

Default branch: `chore/add-license`

_Could not read workflows: no .github/workflows on default branch_

---

## companionintelligence.github.io

Default branch: `main`

### Test locally

```bash
ci-local run --repo companionintelligence.github.io
```

Equivalent to:

```bash
npm ci
npm run --silent build
```

### Run a check workflow on GitHub

**Style canon lint** — `style-canon-lint.yml`

```bash
gh workflow run style-canon-lint.yml --repo companionintelligence/companionintelligence.github.io --ref main
```

---

## GTM-Dashboard

Default branch: `master`

_Could not read workflows: no .github/workflows on default branch_

---

## homebrew-tap

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## JustInCase

Default branch: `main`

### Test locally

_No local checks detected._

### Run a check workflow on GitHub

**CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/JustInCase --ref main
```

**Style canon lint** — `style-canon-lint.yml`

```bash
gh workflow run style-canon-lint.yml --repo companionintelligence/JustInCase --ref main
```

### Cut a release

**Publish container** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/JustInCase --ref main
```

---

## Local-Bench

Default branch: `main`

### Test locally

```bash
ci-local run --repo Local-Bench
```

Equivalent to:

```bash
npm ci
npm run --silent test
npm run --silent build
```

### Cut a release

**Publish container** — `docker-publish.yml`

```bash
gh workflow run docker-publish.yml --repo companionintelligence/Local-Bench --ref main
```

---

## publishing-steelthread

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## scoop-bucket

Default branch: `main`

_Could not read workflows: no .github/workflows on default branch_

---

## systeminformation

Default branch: `master`

### Test locally

```bash
ci-local run --repo systeminformation
```

Equivalent to:

```bash
npm ci
npm run --silent test
```

### Run a check workflow on GitHub

**CodeQL** — `codeql.yml`

```bash
gh workflow run codeql.yml --repo companionintelligence/systeminformation --ref master
```

**Test** — `test.yml`

```bash
gh workflow run test.yml --repo companionintelligence/systeminformation --ref master
```

### Cut a release

**Release** — `release.yml`

```bash
gh workflow run release.yml --repo companionintelligence/systeminformation --ref master
```

---

## Torollo

Default branch: `main`

### Test locally

_No local checks detected._

### Run a check workflow on GitHub

**CI** — `ci.yml`

```bash
gh workflow run ci.yml --repo companionintelligence/Torollo --ref main
```

**Integration** — `integration.yml`

```bash
gh workflow run integration.yml --repo companionintelligence/Torollo --ref main
```

---

## ZARCHIVE-CI-Connect

Default branch: `main`

### Test locally

```bash
ci-local run --repo ZARCHIVE-CI-Connect
```

Equivalent to:

```bash
flutter pub get
flutter analyze
flutter test
```

### Run a check workflow on GitHub

**companion_connect** — `main.yaml`

```bash
gh workflow run main.yaml --repo companionintelligence/ZARCHIVE-CI-Connect --ref main
```

---

## ZARCHIVE-CI-Mobile-Flutter

Default branch: `main`

### Test locally

```bash
ci-local run --repo ZARCHIVE-CI-Mobile-Flutter
```

Equivalent to:

```bash
flutter pub get
flutter analyze
flutter test
```

### Run a check workflow on GitHub

**ci** — `main.yaml`

```bash
gh workflow run main.yaml --repo companionintelligence/ZARCHIVE-CI-Mobile-Flutter --ref main
```

---
