"""Audit what is actually live on each repo's default branch.

Everything else in this package reasons about local clones. Clones go stale the
moment someone merges, and a gating PR only saves money once it lands - so the
question "are we still paying for this?" can only be answered against the
branch GitHub actually evaluates triggers from.

This also catches the drift case: workflows added after a gating pass are born
ungated, so a repo that was fully gated last week can be billing again today.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .policy import MANUAL_TRIGGERS, Policy
from .workflows import PRESERVED_TRIGGERS, WorkflowParseError, declared_triggers, is_gated


@dataclass
class DispatchInput:
    """One ``workflow_dispatch`` input, as a dev must supply it on the CLI."""

    name: str
    required: bool = False
    default: str = ""
    description: str = ""
    type: str = ""

    @property
    def flag(self) -> str:
        value = self.default or f"<{self.type or 'value'}>"
        return f"-f {self.name}={value}"


@dataclass
class RemoteWorkflow:
    name: str
    gated: bool
    triggers: List[str]
    kept: Optional[str] = None
    title: str = ""
    inputs: List["DispatchInput"] = field(default_factory=list)

    @property
    def billable(self) -> List[str]:
        """Triggers that still cause GitHub-hosted runs."""
        return [
            t for t in self.triggers
            if t not in MANUAL_TRIGGERS and t not in PRESERVED_TRIGGERS
        ]

    @property
    def status(self) -> str:
        if self.gated:
            return "gated"
        if not self.billable:
            return "manual"
        if self.kept:
            return "kept"
        return "BILLING"


@dataclass
class RepoAudit:
    repo: str
    base: str = ""
    error: str = ""
    workflows: List[RemoteWorkflow] = field(default_factory=list)

    @property
    def billing(self) -> List[RemoteWorkflow]:
        return [w for w in self.workflows if w.status == "BILLING"]

    @property
    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for wf in self.workflows:
            out[wf.status] = out.get(wf.status, 0) + 1
        return out


_TITLE = re.compile(r"^name:\s*(.+?)\s*$", re.M)


def workflow_title(text: str) -> str:
    """The workflow's display name - what shows up in the Actions tab."""
    match = _TITLE.search(text)
    if not match:
        return ""
    return match.group(1).strip().strip("\"'")


def dispatch_inputs(text: str) -> List[DispatchInput]:
    """Parse ``workflow_dispatch.inputs`` well enough to document the CLI call.

    Hand-rolled rather than via a YAML parse because gating leaves the original
    triggers behind as comments, and a parser would either choke on the
    commented block or, worse, silently read inputs out of it.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("#"):
            continue
        if re.match(r"^\s*workflow_dispatch\s*:", line):
            start = i
            break
    if start is None:
        return []

    dispatch_indent = len(lines[start]) - len(lines[start].lstrip())
    inputs_at = None
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= dispatch_indent:
            break
        if re.match(r"^\s*inputs\s*:", line):
            inputs_at = i
            break
    if inputs_at is None:
        return []

    inputs_indent = len(lines[inputs_at]) - len(lines[inputs_at].lstrip())
    out: List[DispatchInput] = []
    current: Optional[DispatchInput] = None
    key_indent = None
    for i in range(inputs_at + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= inputs_indent:
            break
        match = re.match(r"^\s*([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not match:
            continue
        if key_indent is None:
            key_indent = indent
        if indent == key_indent:
            current = DispatchInput(name=match.group(1))
            out.append(current)
            continue
        if current is None:
            continue
        field_name, value = match.group(1), match.group(2).strip().strip("\"'")
        if field_name == "required":
            current.required = value.lower() == "true"
        elif field_name == "default":
            current.default = value
        elif field_name == "description":
            current.description = value
        elif field_name == "type":
            current.type = value
    return out


def _gh(args: List[str]) -> Tuple[int, str]:
    proc = subprocess.run(["gh"] + args, capture_output=True, text=True)
    return proc.returncode, proc.stdout


def audit_repo(repo_slug: str, policy: Policy, local_name: str = "") -> RepoAudit:
    """Read every workflow on ``repo_slug``'s default branch and classify it."""
    local_name = local_name or repo_slug.split("/")[-1]
    audit = RepoAudit(local_name)

    code, out = _gh(["repo", "view", repo_slug, "--json", "defaultBranchRef",
                     "-q", ".defaultBranchRef.name"])
    if code or not out.strip():
        audit.error = "cannot resolve default branch"
        return audit
    audit.base = out.strip()

    code, out = _gh(["api", f"repos/{repo_slug}/contents/.github/workflows"
                             f"?ref={audit.base}"])
    if code:
        audit.error = "no .github/workflows on default branch"
        return audit
    try:
        entries = json.loads(out)
    except json.JSONDecodeError:
        audit.error = "unreadable workflow listing"
        return audit
    if not isinstance(entries, list):
        audit.error = "unreadable workflow listing"
        return audit

    names = [
        e["name"] for e in entries
        if e.get("type") == "file" and e.get("name", "").endswith((".yml", ".yaml"))
    ]

    def fetch(name: str) -> Optional[RemoteWorkflow]:
        code, body = _gh(["api", f"repos/{repo_slug}/contents/"
                                 f".github/workflows/{name}?ref={audit.base}",
                          "-q", ".content"])
        if code or not body.strip():
            return None
        try:
            text = base64.b64decode(body).decode("utf-8", errors="replace")
        except ValueError:
            return None
        try:
            triggers = declared_triggers(text)
        except WorkflowParseError:
            triggers = []
        return RemoteWorkflow(
            name=name,
            gated=is_gated(text),
            triggers=triggers,
            kept=policy.matches_keep(f"{local_name}/{name}"),
            title=workflow_title(text) or name,
            inputs=dispatch_inputs(text),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        audit.workflows = [w for w in pool.map(fetch, names) if w]
    audit.workflows.sort(key=lambda w: w.name)
    return audit


def audit_worktree(
    worktree, local_name: str, policy: Policy, base: str
) -> RepoAudit:
    """Same classification as :func:`audit_repo`, read off disk.

    ``ship`` already has a checkout in hand; going back to the API would be
    slower and could disagree with the branch it is about to commit.
    """
    from .workflows import read_workflow

    audit = RepoAudit(local_name, base)
    wf_dir = worktree / ".github" / "workflows"
    if not wf_dir.is_dir():
        return audit
    for path in sorted(wf_dir.iterdir()):
        if not path.is_file() or path.suffix not in (".yml", ".yaml"):
            continue
        try:
            text = read_workflow(path)
        except OSError:
            continue
        try:
            triggers = declared_triggers(text)
        except WorkflowParseError:
            triggers = []
        audit.workflows.append(RemoteWorkflow(
            name=path.name,
            gated=is_gated(text),
            triggers=triggers,
            kept=policy.matches_keep(f"{local_name}/{path.name}"),
            title=workflow_title(text) or path.name,
            inputs=dispatch_inputs(text),
        ))
    return audit


def audit_all(
    repos: List[Tuple[str, str]], policy: Policy, workers: int = 6
) -> List[RepoAudit]:
    """``repos`` is a list of ``(local_name, owner/repo)`` pairs."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(
            lambda pair: audit_repo(pair[1], policy, pair[0]), repos
        ))


# --- drift detection -------------------------------------------------------
#
# A gating pass is a snapshot, not a policy. Workflows added later are born
# with automatic triggers, and a repo that verified clean last week can be
# billing again today with nobody noticing. The baseline records what we
# expect; drift is anything that got worse.

BASELINE_FILE = ".ci-local-baseline.json"


def snapshot(audits: List[RepoAudit]) -> Dict[str, Dict]:
    """Reduce an audit to the facts drift cares about."""
    out: Dict[str, Dict] = {}
    for audit in audits:
        if audit.error:
            continue
        out[audit.repo] = {
            "base": audit.base,
            "billing": sorted(w.name for w in audit.billing),
            "known": sorted(w.name for w in audit.workflows),
        }
    return out


@dataclass
class Drift:
    repo: str
    kind: str  # "regressed" | "new-workflow" | "new-repo"
    workflows: List[str] = field(default_factory=list)

    def describe(self) -> str:
        joined = ", ".join(self.workflows)
        if self.kind == "new-workflow":
            return f"{self.repo}: new ungated workflow(s) {joined}"
        if self.kind == "new-repo":
            return f"{self.repo}: not in baseline, billing on {joined}"
        return f"{self.repo}: regained automatic triggers on {joined}"


def find_drift(baseline: Dict[str, Dict], current: Dict[str, Dict]) -> List[Drift]:
    """Report every repo that is billing more than the baseline allowed.

    Improvements are never drift - a repo that gated more workflows since the
    baseline should not page anyone.
    """
    drifts: List[Drift] = []
    for repo, now in sorted(current.items()):
        billing_now = set(now["billing"])
        if not billing_now:
            continue
        was = baseline.get(repo)
        if was is None:
            drifts.append(Drift(repo, "new-repo", sorted(billing_now)))
            continue

        allowed = set(was["billing"])
        newly_billing = billing_now - allowed
        if not newly_billing:
            continue

        # Distinguish a workflow that did not exist at baseline time (drift by
        # addition) from one that was gated and has since been un-gated.
        known_before = set(was.get("known", []))
        added = sorted(w for w in newly_billing if w not in known_before)
        regressed = sorted(w for w in newly_billing if w in known_before)
        if added:
            drifts.append(Drift(repo, "new-workflow", added))
        if regressed:
            drifts.append(Drift(repo, "regressed", regressed))
    return drifts
