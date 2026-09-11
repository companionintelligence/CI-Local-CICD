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

from .activity import DEFAULT_WINDOW_DAYS as ACTIVITY_WINDOW_DAYS
from .activity import RunActivity, fetch_activity
from .billing import BillingVerdict, classify_billing
from .policy import MANUAL_TRIGGERS, Policy
from .spend import (
    GATED_BILLABLE,
    LIVE_BILLING,
    SpendState,
    classify_spend,
    linux_equivalent,
)
from .workflows import (
    PRESERVED_TRIGGERS,
    WorkflowParseError,
    declared_triggers,
    is_gated,
    restored_triggers,
    runner_labels,
    runner_targets,
)


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
    # Triggers the workflow would fire on if gating were lifted. For an ungated
    # workflow this equals `triggers`; for a gated one it is recovered from the
    # marker comments, which is the only place the real answer survives.
    restored: List[str] = field(default_factory=list)
    # Flattened labels, for display.
    runners: List[str] = field(default_factory=list)
    # One label group per `runs-on:`; grouping is what distinguishes a
    # self-hosted job from a hosted one. Defaults to one group per label.
    targets: List[List[str]] = field(default_factory=list)
    # Private unless proven otherwise: over-reporting the bill is the safe error.
    private: bool = True
    # Run history, when someone asked for it. ``None`` means "not measured",
    # which is not the same as "never runs" and must not be reported as such.
    activity: Optional[RunActivity] = None

    def __post_init__(self) -> None:
        if not self.restored:
            self.restored = list(self.triggers)
        if not self.targets:
            self.targets = [[label] for label in self.runners]

    @property
    def billable(self) -> List[str]:
        """Automatic triggers that are live *right now*.

        Empty for a gated workflow, correctly: gating stopped the spend. Use
        :attr:`restored_billable` to ask what it would cost if turned back on.
        """
        return [
            t for t in self.triggers
            if t not in MANUAL_TRIGGERS and t not in PRESERVED_TRIGGERS
        ]

    @property
    def restored_billable(self) -> List[str]:
        """Automatic triggers this workflow has when not gated."""
        return [
            t for t in self.restored
            if t not in MANUAL_TRIGGERS and t not in PRESERVED_TRIGGERS
        ]

    @property
    def verdict(self) -> BillingVerdict:
        """Would this cost money if it were running? Independent of gating.

        Deliberately computed from :attr:`restored`, not :attr:`triggers`, and
        deliberately never consults :attr:`gated`. A gated workflow keeps a
        truthful cost answer so restoring it is a priced decision.
        """
        return classify_billing(self.restored, self.targets, self.private)

    @property
    def would_bill(self) -> bool:
        return self.verdict.billable

    @property
    def spend(self) -> SpendState:
        """Where this workflow sits on all three axes - the honest answer.

        Use this, not :attr:`status`, for any question about money.
        """
        return classify_spend(
            triggers_live=bool(self.billable),
            verdict=self.verdict,
            gated=self.gated,
            activity=self.activity,
            kept=self.kept,
        )

    @property
    def is_billing_now(self) -> bool:
        """Automatic triggers are live and they cost money. Kept or not."""
        return self.spend.is_billing_now

    @property
    def is_restore_cost(self) -> bool:
        """Gated, and would resume spend if restored. Excludes live workflows."""
        return self.spend.is_restore_cost

    @property
    def status(self) -> str:
        """Display bucket for the audit table. **Not** a spend classification.

        ``"kept"`` is a *policy* label - "we chose not to gate this" - and says
        nothing about whether it costs money. Reading a non-``"BILLING"``
        status as "not billing" is what put CI-Portal's live production
        ``deploy.yml`` in the dormant column. :attr:`spend` is the axis-correct
        answer; this stays for the table, the runbook and the drift baseline.
        """
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
    private: bool = True

    @property
    def billing(self) -> List[RemoteWorkflow]:
        """Ungated, automatic, billable and *not* on the keep list.

        Despite the name this is the **policy-violation** set - "what is still
        billing that we said we would gate" - not the bill. It deliberately
        excludes kept workflows, which do bill. It keeps its exact membership
        because :func:`find_drift` compares it against a recorded baseline.
        For money, use :attr:`live_billing`.
        """
        return [w for w in self.workflows
                if w.is_billing_now and not w.kept]

    @property
    def live_billing(self) -> List[RemoteWorkflow]:
        """Everything putting minutes on the invoice today, kept included.

        The number the cost decision needs.
        """
        return [w for w in self.workflows if w.is_billing_now]

    @property
    def restore_cost(self) -> List[RemoteWorkflow]:
        """Gated workflows that would resume spend if restored.

        Strictly disjoint from :attr:`live_billing`: a workflow cannot be both
        switched off and running. That disjointness is the fix - the merged
        report built this list as "billable and not status BILLING", which is
        neither.
        """
        return [w for w in self.workflows if w.is_restore_cost]

    @property
    def linux_equivalent(self) -> int:
        """Live billing weighted by runner OS. One macOS run costs ten Linux."""
        return linux_equivalent(w.spend for w in self.live_billing)

    @property
    def billable_if_restored(self) -> List[RemoteWorkflow]:
        """Workflows that cost money whenever they are not gated.

        Superset of :attr:`billing`. This is the honest answer to "what would
        restoring this repo cost", which :attr:`billing` cannot give because a
        gated workflow reports no live triggers.
        """
        return [w for w in self.workflows if w.would_bill]

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

    # Visibility comes back on the same call: a public repo's standard-runner
    # minutes are free, so the bill cannot be judged without it.
    code, out = _gh(["repo", "view", repo_slug, "--json",
                     "defaultBranchRef,isPrivate"])
    if code or not out.strip():
        audit.error = "cannot resolve default branch"
        return audit
    try:
        meta = json.loads(out)
        audit.base = (meta.get("defaultBranchRef") or {}).get("name", "")
    except json.JSONDecodeError:
        audit.error = "cannot resolve default branch"
        return audit
    if not audit.base:
        audit.error = "cannot resolve default branch"
        return audit
    audit.private = bool(meta.get("isPrivate", True))

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
            restored=restored_triggers(text),
            runners=runner_labels(text),
            targets=runner_targets(text),
            private=audit.private,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        audit.workflows = [w for w in pool.map(fetch, names) if w]
    audit.workflows.sort(key=lambda w: w.name)
    return audit


def audit_worktree(
    worktree, local_name: str, policy: Policy, base: str, private: bool = True
) -> RepoAudit:
    """Same classification as :func:`audit_repo`, read off disk.

    ``ship`` already has a checkout in hand; going back to the API would be
    slower and could disagree with the branch it is about to commit.
    """
    from .workflows import read_workflow

    audit = RepoAudit(local_name, base, private=private)
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
            restored=restored_triggers(text),
            runners=runner_labels(text),
            targets=runner_targets(text),
            private=audit.private,
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


def measure_activity(
    audits: List[RepoAudit],
    slugs: Dict[str, str],
    window_days: int = ACTIVITY_WINDOW_DAYS,
    states: Tuple[str, ...] = (LIVE_BILLING, GATED_BILLABLE),
    fetch=fetch_activity,
    workers: int = 8,
) -> List[RepoAudit]:
    """Fill in :attr:`RemoteWorkflow.activity` for the workflows worth pricing.

    One API call per workflow, so it is opt-in and scoped: by default only the
    states where the answer changes a decision - what is billing now, and what
    would bill if restored. Everything left alone keeps ``activity=None``,
    which reports as "not checked" rather than pretending to be dormant.
    """
    jobs: List[Tuple[RemoteWorkflow, str, str]] = []
    for audit in audits:
        if audit.error:
            continue
        slug_name = slugs.get(audit.repo)
        if not slug_name:
            continue
        for wf in audit.workflows:
            if wf.spend.state in states:
                jobs.append((wf, slug_name, audit.base))

    def run(job):
        wf, slug_name, _base = job
        wf.activity = fetch(slug_name, wf.name, window_days=window_days)

    if jobs:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run, jobs))
    return audits


# --- drift detection -------------------------------------------------------
#
# A gating pass is a snapshot, not a policy. Workflows added later are born
# with automatic triggers, and a repo that verified clean last week can be
# billing again today with nobody noticing. The baseline records what we
# expect; drift is anything that got worse.

BASELINE_FILE = ".ci-local-baseline.json"


def snapshot(audits: List[RepoAudit]) -> Dict[str, Dict]:
    """Reduce an audit to the facts drift cares about.

    ``billing`` is the policy-violation set and is what :func:`find_drift`
    compares - unchanged, so old baselines stay comparable.

    The other three are the axes kept apart:

    ``live_billing``   - costing money now, kept workflows included. The honest
                         bill, and a superset of ``billing``.
    ``restore_cost``   - gated and would bill again if restored. Disjoint from
                         ``live_billing``.
    ``billable_if_restored`` - the union: every workflow that costs money
                         whenever it is not switched off.
    """
    out: Dict[str, Dict] = {}
    for audit in audits:
        if audit.error:
            continue
        out[audit.repo] = {
            "base": audit.base,
            "billing": sorted(w.name for w in audit.billing),
            "live_billing": sorted(w.name for w in audit.live_billing),
            "restore_cost": sorted(w.name for w in audit.restore_cost),
            "billable_if_restored": sorted(
                w.name for w in audit.billable_if_restored
            ),
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
