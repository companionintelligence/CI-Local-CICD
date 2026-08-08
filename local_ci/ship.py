"""Turn local gating into a reviewable pull request, per repo.

The workspace clones are shared: they sit on feature branches, they are dirty,
and other agents are working in them. So this never commits from a clone.
Instead it creates a throwaway worktree off ``origin/<default branch>``, gates
the workflows *there*, and pushes that. The clone is left exactly as found.

Resolving the default branch matters more than it looks. CI-Hub and
CI-Marketplace default to ``dev``, not ``main``; anything that hard-codes
``main`` silently opens a PR against the wrong base or fails outright.

Git and ``gh`` are reached through an injected runner so the command sequence
can be tested without a network or a real repository.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .concurrency import add_concurrency
from .detect import plan_for
from .docs import render_repo_doc
from .policy import Policy, classify
from .retention import apply_retention
from .verify import audit_worktree
from .workflows import WorkflowParseError, gate, read_workflow, write_workflow

BRANCH = "chore/gate-actions-to-manual"
PR_TITLE = "chore(ci): gate automatic workflow triggers to manual"
CONCURRENCY_TITLE = "chore(ci): cancel superseded workflow runs"
DOCS_TITLE = "docs(ci): how to run tests and releases now that triggers are manual"
DOCS_BODY = """\
Automatic workflow triggers in this repo are gated, so pushing no longer starts
a GitHub-hosted run. `docs/CI.md` records what replaced it: the command to run
this repo's checks on local hardware, and the exact `gh workflow run` line for
every manual workflow including its required inputs.

Generated from this branch's workflow files by `ci-local docs`.
"""
DOC_FILE = "docs/CI.md"
CONCURRENCY_BODY = """\
Without a `concurrency` block, pushing three commits to a branch runs the whole
suite three times and pays for all three. The group is scoped to the ref so
different branches never cancel each other.

Deploy and release workflows use `cancel-in-progress: false`: interrupting one
mid-flight can leave a half-published artifact, so they queue instead.
"""

COMMIT_BODY = """\
Automatic triggers (push/pull_request/schedule) are replaced with
workflow_dispatch so these workflows stop consuming billed GitHub-hosted
minutes. The original trigger block is preserved as a comment above the
replacement; `ci-local restore` puts it back exactly.

workflow_dispatch inputs and workflow_call are carried across unchanged, so
manual runs keep their form and reusable workflows keep working for callers.

Checks move to local hardware via `ci-local run` and a pre-push hook.
"""


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


Runner = Callable[[Sequence[str], Optional[Path]], CommandResult]


def real_runner(argv: Sequence[str], cwd: Optional[Path] = None) -> CommandResult:
    proc = subprocess.run(
        list(argv), cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


@dataclass
class ShipResult:
    repo: str
    status: str  # shipped | dry-run | skipped | failed
    detail: str = ""
    branch: str = BRANCH
    base: str = ""
    workflows: List[str] = field(default_factory=list)
    retention: List[str] = field(default_factory=list)
    concurrency: List[str] = field(default_factory=list)
    docs: bool = False
    pr_url: str = ""
    commands: List[str] = field(default_factory=list)


def slug(url: str) -> str:
    """``owner/repo`` from a git remote URL, for `gh --repo`."""
    cleaned = url.strip()
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    if cleaned.startswith("git@"):
        cleaned = cleaned.split(":", 1)[-1]
    else:
        for prefix in ("https://github.com/", "http://github.com/", "ssh://git@github.com/"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break
    return "/".join(cleaned.split("/")[-2:])


def default_branch(repo_slug: str, repo_path: Path, run: Runner) -> Optional[str]:
    """Ask GitHub, then fall back to the clone's origin/HEAD.

    Never guess ``main``: two of these repos default to ``dev``, and a wrong
    base makes the PR either fail or target the wrong branch.
    """
    result = run(
        ["gh", "repo", "view", repo_slug, "--json", "defaultBranchRef",
         "-q", ".defaultBranchRef.name"],
        repo_path,
    )
    if result.ok and result.stdout.strip():
        return result.stdout.strip()

    result = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                 repo_path)
    if result.ok and result.stdout.strip():
        return result.stdout.strip().split("/", 1)[-1]
    return None


def gate_worktree(
    worktree: Path,
    repo_name: str,
    policy: Policy,
    retention_days: Optional[int] = None,
    concurrency: bool = False,
    gating: bool = True,
) -> Tuple[List[str], List[str], List[str]]:
    """Gate eligible workflows in a worktree, and optionally cap retention.

    Retention is applied to *every* workflow, including ones kept on their
    automatic triggers: storage is billed wherever a job ran, so capping it is
    orthogonal to gating and safe on the deploy and release paths too.

    Returns ``(gated, retention_capped, concurrency_added)`` file-name lists.
    """
    wf_dir = worktree / ".github" / "workflows"
    if not wf_dir.is_dir():
        return [], [], []

    gated, capped, concurrent = [], [], []
    for path in sorted(wf_dir.iterdir()):
        if not path.is_file() or path.suffix not in (".yml", ".yaml"):
            continue
        text = read_workflow(path)

        try:
            decision = classify(repo_name, path.name, text, policy)
            if gating and decision.will_change:
                new_text, did = gate(text)
                if did:
                    text = new_text
                    gated.append(path.name)
        except WorkflowParseError:
            pass  # not a gateable workflow; retention may still apply

        if retention_days is not None:
            new_text, changes = apply_retention(text, days=retention_days)
            if changes:
                text = new_text
                capped.append(path.name)

        if concurrency:
            new_text, change = add_concurrency(text, path.name)
            if change:
                text = new_text
                concurrent.append(path.name)

        if path.name in gated or path.name in capped or path.name in concurrent:
            write_workflow(path, text)
    return gated, capped, concurrent


def pr_body(
    repo: str, workflows: List[str], saved: float,
    capped: Optional[List[str]] = None, concurrent: Optional[List[str]] = None,
    docs: bool = False,
) -> str:
    capped, concurrent = capped or [], concurrent or []
    if docs and not workflows and not concurrent and not capped:
        return (f"## What\n\nAdds `{DOC_FILE}`.\n\n{DOCS_BODY}\n"
                "## Why generated\n\nA hand-written version goes stale the "
                "first time someone adds a workflow. Regenerate with "
                "`ci-local docs`.\n")
    lines = ["## What", ""]
    if concurrent:
        lines += [
            f"- Adds a `concurrency` block to {len(concurrent)} workflow(s) so "
            "superseded runs are cancelled instead of running to completion. "
            "Deploy and release paths get `cancel-in-progress: false` - they "
            "queue rather than being interrupted mid-flight.",
        ]
    if workflows:
        lines += [
            f"- Gates {len(workflows)} workflow(s) down to `workflow_dispatch` "
            "(manual runs only).",
        ]
    if capped:
        lines += [
            f"- Caps artifact `retention-days` on {len(capped)} workflow(s). "
            "Actions storage is billed wherever the job ran, so gating alone "
            "does not reduce it.",
        ]
    lines += [""]
    if saved:
        lines += [
            f"Attributed GitHub Actions spend for these workflows: "
            f"**${saved:,.2f}/month** (July 2026 usage report).",
            "",
        ]
    lines += ["## Why", "", COMMIT_BODY]
    if workflows:
        lines += ["", "## Gated workflows", ""]
        lines += [f"- `{name}`" for name in workflows]
    if capped:
        lines += ["", "## Retention capped", ""]
        lines += [f"- `{name}`" for name in capped]
    if concurrent:
        lines += ["", "## Concurrency added", ""]
        lines += [f"- `{name}`" for name in concurrent]
    lines += [
        "",
        "## Reverting",
        "",
        "The original triggers are preserved verbatim as a comment block above "
        "each replacement:",
        "",
        "```bash",
        "ci-local restore --repo " + repo + " --apply",
        "```",
        "",
        "Deploy-on-merge and tag-triggered release workflows are deliberately "
        "left on their automatic triggers.",
    ]
    return "\n".join(lines)


def commit_message(
    workflows: List[str], capped: Optional[List[str]] = None,
    concurrent: Optional[List[str]] = None, docs: bool = False,
) -> str:
    capped, concurrent = capped or [], concurrent or []
    if docs and not workflows and not concurrent and not capped:
        return f"{DOCS_TITLE}\n\n{DOCS_BODY}"
    if concurrent and not workflows:
        subject = CONCURRENCY_TITLE
        body = CONCURRENCY_BODY
        tail = f"Added a concurrency block to {len(concurrent)} workflow(s)."
    else:
        subject, body = PR_TITLE, COMMIT_BODY
        tail = f"Gated {len(workflows)} workflow(s)."
        if concurrent:
            tail += f" Added concurrency to {len(concurrent)}."
    if capped:
        tail += f" Capped artifact retention on {len(capped)}."
    if docs:
        tail += f" Documented in {DOC_FILE}."
    return f"{subject}\n\n{body}\n{tail}"


def ship_repo(
    repo_path: Path,
    policy: Policy,
    worktree_root: Path,
    usage: Optional[Dict] = None,
    branch: str = BRANCH,
    retention_days: Optional[int] = None,
    concurrency: bool = False,
    gating: bool = True,
    write_docs: bool = False,
    apply: bool = False,
    run: Runner = real_runner,
) -> ShipResult:
    """Gate one repo on a clean worktree and open a PR for it."""
    repo = repo_path.name
    result = ShipResult(repo, "skipped")

    remote = run(["git", "remote", "get-url", "origin"], repo_path)
    if not remote.ok:
        result.detail = "no origin remote"
        return result
    repo_slug = slug(remote.stdout)

    base = default_branch(repo_slug, repo_path, run)
    if not base:
        result.status = "failed"
        result.detail = "could not resolve default branch"
        return result
    result.base = base

    fetched = run(["git", "fetch", "origin", base, "--quiet"], repo_path)
    if not fetched.ok:
        result.status = "failed"
        result.detail = f"fetch failed: {fetched.stderr.strip()[:200]}"
        return result

    worktree = worktree_root / repo
    # A leftover worktree from an interrupted run would otherwise fail the add.
    run(["git", "worktree", "remove", "--force", str(worktree)], repo_path)
    added = run(
        ["git", "worktree", "add", "--force", "-B", branch, str(worktree),
         f"origin/{base}"],
        repo_path,
    )
    if not added.ok:
        result.status = "failed"
        result.detail = f"worktree failed: {added.stderr.strip()[:200]}"
        return result

    try:
        changed, capped, concurrent = gate_worktree(
            worktree, repo, policy, retention_days, concurrency, gating
        )
        result.workflows = changed
        result.retention = capped
        result.concurrency = concurrent

        if write_docs:
            audit = audit_worktree(worktree, repo, policy, base)
            if audit.workflows:
                doc = worktree / DOC_FILE
                doc.parent.mkdir(parents=True, exist_ok=True)
                text = render_repo_doc(
                    audit, plan_for(repo, worktree, policy.override_for(repo)),
                    repo_slug.split("/")[-1],
                )
                if not doc.exists() or doc.read_text(encoding="utf-8") != text:
                    doc.write_text(text, encoding="utf-8")
                    result.docs = True

        if not changed and not capped and not concurrent and not result.docs:
            result.detail = f"nothing to change on {base}"
            return result

        saved = 0.0
        if usage:
            github_name = policy.github_name(repo)
            saved = sum(usage.get((github_name, name), 0.0) for name in changed)

        commands = [
            ["git", "add", "--", ".github/workflows", DOC_FILE]
            if write_docs else ["git", "add", "--", ".github/workflows"],
            # --no-verify here too: a fresh worktree has no node_modules, so
            # a husky/lint-staged pre-commit hook fails outright. This commit is
            # workflow YAML only and never needs those hooks.
            ["git", "commit", "--no-verify", "-m",
             commit_message(changed, capped, concurrent, result.docs)],
            # Likewise for push: the pre-push hook we install runs the repo's
            # whole lint/test suite, so shipping would deadlock on its own hook.
            ["git", "push", "--no-verify", "--force-with-lease", "-u",
             "origin", branch],
        ]
        result.commands = [" ".join(c[:4]) for c in commands]

        if not apply:
            result.status = "dry-run"
            result.detail = f"{len(changed)} workflow(s) off {base}"
            return result

        for argv in commands:
            step = run(argv, worktree)
            if not step.ok:
                result.status = "failed"
                result.detail = f"{argv[1]} failed: {step.stderr.strip()[:200]}"
                return result

        pr = run(
            ["gh", "pr", "create", "--repo", repo_slug, "--base", base,
             "--head", branch, "--title", PR_TITLE,
             "--body", pr_body(repo, changed, saved, capped, concurrent,
                               result.docs)],
            worktree,
        )
        if not pr.ok:
            combined = (pr.stderr + pr.stdout).strip()
            if "already exists" in combined:
                result.status = "shipped"
                result.detail = "branch pushed; PR already open"
                return result
            result.status = "failed"
            result.detail = f"pr create failed: {combined[:200]}"
            return result

        result.status = "shipped"
        result.pr_url = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else ""
        result.detail = result.pr_url
        return result
    finally:
        if worktree.exists():
            removed = run(
                ["git", "worktree", "remove", "--force", str(worktree)], repo_path
            )
            if not removed.ok:
                shutil.rmtree(worktree, ignore_errors=True)
            run(["git", "worktree", "prune"], repo_path)
