"""Generate the developer runbook.

Gating changed the contract for every repo: checks that used to happen because
you pushed now happen because you ask for them. That is only an improvement if
people know what to ask for, so this renders - per repo - the local command to
run checks, and the exact ``gh workflow run`` line for each manual workflow,
including its required inputs.

Generated rather than hand-written, because a hand-written version of this goes
stale the first time someone adds a workflow. Regenerate with `ci-local docs`.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .detect import RepoPlan
from .verify import RepoAudit, RemoteWorkflow

# Workflows whose whole job is releasing. Called out separately because
# "how do I cut a release now?" is the question gating most changes the answer
# to, and burying it in a list of twenty test workflows helps nobody.
RELEASE_HINTS = ("release", "publish", "deploy", "pages", "tag")

HEADER = """\
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

# Install the pre-push hook so you cannot forget
ci-local hooks install
```

To start a gated workflow on GitHub you need the `gh` CLI and push access:

```bash
gh workflow run <file>.yml --repo companionintelligence/<REPO> --ref <branch>
```

`--ref` matters: the workflow file is read from that branch, so a workflow that
only exists on your feature branch must be dispatched against it.

Watch it with `gh run watch --repo companionintelligence/<REPO>`.

---
"""


def is_release(workflow: RemoteWorkflow) -> bool:
    name = workflow.name.lower()
    return any(hint in name for hint in RELEASE_HINTS)


def dispatch_command(repo_slug: str, workflow: RemoteWorkflow, ref: str) -> str:
    parts = [
        "gh workflow run", workflow.name,
        f"--repo companionintelligence/{repo_slug}", f"--ref {ref}",
    ]
    parts += [inp.flag for inp in workflow.inputs]
    return " ".join(parts)


def _local_section(plan: Optional[RepoPlan]) -> List[str]:
    if plan is None or not plan.checks:
        return ["_No local checks detected._", ""]
    lines = ["```bash", f"ci-local run --repo {plan.repo}", "```", "",
             "Equivalent to:", "", "```bash"]
    if plan.install:
        lines.append(plan.install.display)
    for check in plan.checks:
        suffix = "" if check.required else "   # optional"
        lines.append(f"{check.display}{suffix}")
    lines += ["```", ""]
    return lines


def render_repo(
    audit: RepoAudit, plan: Optional[RepoPlan], slug: str
) -> List[str]:
    lines = [f"## {audit.repo}", "", f"Default branch: `{audit.base}`", ""]

    if audit.error:
        return lines + [f"_Could not read workflows: {audit.error}_", ""]

    lines += ["### Test locally", ""] + _local_section(plan)

    manual = [w for w in audit.workflows if w.status in ("gated", "manual")]
    releases = [w for w in manual if is_release(w)]
    checks = [w for w in manual if not is_release(w)]

    if checks:
        lines += ["### Run a check workflow on GitHub", ""]
        for wf in sorted(checks, key=lambda w: w.name):
            lines += [f"**{wf.title}** — `{wf.name}`", "", "```bash",
                      dispatch_command(slug, wf, audit.base), "```", ""]
            required = [i for i in wf.inputs if i.required]
            if required:
                lines += ["Required inputs: "
                          + ", ".join(f"`{i.name}`"
                                      + (f" ({i.description})" if i.description else "")
                                      for i in required), ""]

    if releases:
        lines += ["### Cut a release", ""]
        for wf in sorted(releases, key=lambda w: w.name):
            lines += [f"**{wf.title}** — `{wf.name}`", "", "```bash",
                      dispatch_command(slug, wf, audit.base), "```", ""]
            required = [i for i in wf.inputs if i.required]
            if required:
                lines += ["Required inputs: "
                          + ", ".join(f"`{i.name}`"
                                      + (f" ({i.description})" if i.description else "")
                                      for i in required), ""]

    automatic = [w for w in audit.workflows if w.status in ("BILLING", "kept")]
    if automatic:
        lines += ["### Still automatic", "",
                  "These run without being asked - no action needed:", ""]
        for wf in sorted(automatic, key=lambda w: w.name):
            trig = ", ".join(wf.billable) or "-"
            lines.append(f"- `{wf.name}` on {trig}")
        lines.append("")

    if not manual and not automatic:
        lines += ["_No workflows._", ""]
    return lines


def render(
    audits: Sequence[RepoAudit],
    plans: Dict[str, RepoPlan],
    slugs: Dict[str, str],
) -> str:
    body: List[str] = [HEADER]
    usable = [a for a in audits if a.workflows or a.error]
    for audit in sorted(usable, key=lambda a: a.repo.lower()):
        body += render_repo(audit, plans.get(audit.repo),
                            slugs.get(audit.repo, audit.repo))
        body.append("---")
        body.append("")
    return "\n".join(body).rstrip() + "\n"
