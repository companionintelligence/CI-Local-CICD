"""Decide which workflows get gated to manual and which keep their triggers.

The default stance is "gate it" - the point of this tooling is to stop paying
for hosted minutes. The one override is a ``keep`` pattern in ``ci-local.toml``,
seeded with the deploy and release paths, because those are how software
actually ships and they are a small slice of the bill.

``workflow_call`` is deliberately *not* a reason to keep a workflow. A reusable
workflow that also fires on ``push`` is two things wearing one hat, and only the
push half costs money: :func:`local_ci.workflows.gate` carries ``workflow_call``
through untouched, so callers keep working while the automatic runs stop. In
this workspace that distinction is worth ~$46/month on CI-Hub alone.
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .workflows import PRESERVED_TRIGGERS, declared_triggers, is_gated

DEFAULT_POLICY_FILE = "ci-local.toml"

# Substrings that mark a workflow as part of the ship path rather than the test
# path. Used only to explain a decision and to seed a new policy file.
SHIP_HINTS = ("deploy", "release", "publish", "pages", "provenance")

MANUAL_TRIGGERS = {"workflow_dispatch", "repository_dispatch"}


@dataclass
class Policy:
    """Which workflows to leave alone. Patterns are ``repo/workflow.yml`` globs."""

    keep: List[str] = field(default_factory=list)
    force_gate: List[str] = field(default_factory=list)
    # Local clone directory -> GitHub repository name. Renames on GitHub do not
    # rename the clone, and the billing report only knows the GitHub name.
    aliases: Dict[str, str] = field(default_factory=dict)
    # Clone directories to ignore entirely - orphaned clones whose remote is
    # gone, vendored copies, anything that is not a live repo.
    exclude: List[str] = field(default_factory=list)
    checks: Dict[str, "RepoOverride"] = field(default_factory=dict)
    source: Optional[Path] = None

    @classmethod
    def load(cls, path: Path) -> "Policy":
        if not path.exists():
            return cls(source=None)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        section = data.get("workflows", {})
        return cls(
            keep=list(section.get("keep", [])),
            force_gate=list(section.get("force_gate", [])),
            aliases=dict(data.get("repos", {}).get("aliases", {})),
            exclude=list(data.get("repos", {}).get("exclude", [])),
            checks={
                name: RepoOverride.from_dict(value)
                for name, value in data.get("checks", {}).items()
            },
            source=path,
        )

    def github_name(self, local_repo: str) -> str:
        """The GitHub repository name for a local clone directory."""
        return self.aliases.get(local_repo, local_repo)

    def is_excluded(self, local_repo: str) -> bool:
        return _first_match(local_repo, self.exclude) is not None

    def override_for(self, local_repo: str) -> Optional["RepoOverride"]:
        return self.checks.get(local_repo)

    def matches_keep(self, ident: str) -> Optional[str]:
        return _first_match(ident, self.keep)

    def matches_force_gate(self, ident: str) -> Optional[str]:
        return _first_match(ident, self.force_gate)


def _first_match(ident: str, patterns: List[str]) -> Optional[str]:
    for pattern in patterns:
        if fnmatch.fnmatch(ident, pattern):
            return pattern
    return None


@dataclass
class RepoOverride:
    """Per-repo corrections for repos the detector cannot read off disk.

    ``workdir`` handles repos whose real project lives in a subdirectory - the
    root manifest is a decoy, and running there produces confident nonsense.
    ``commands`` replaces the detected set outright.
    """

    workdir: Optional[str] = None
    commands: List[Dict[str, object]] = field(default_factory=list)
    skip: bool = False

    @classmethod
    def from_dict(cls, data: Dict) -> "RepoOverride":
        return cls(
            workdir=data.get("workdir"),
            commands=list(data.get("commands", [])),
            skip=bool(data.get("skip", False)),
        )


@dataclass
class Decision:
    repo: str
    workflow: str
    action: str  # "gate" | "keep" | "already-gated" | "already-manual"
    reason: str
    triggers: List[str]

    @property
    def ident(self) -> str:
        return f"{self.repo}/{self.workflow}"

    @property
    def will_change(self) -> bool:
        return self.action == "gate"


def classify(repo: str, workflow: str, text: str, policy: Policy) -> Decision:
    """Work out what should happen to one workflow file."""
    ident = f"{repo}/{workflow}"
    triggers = declared_triggers(text)

    if is_gated(text):
        return Decision(repo, workflow, "already-gated", "gated previously", triggers)

    billable = [
        t
        for t in triggers
        if t not in MANUAL_TRIGGERS and t not in PRESERVED_TRIGGERS
    ]
    if not billable:
        return Decision(
            repo, workflow, "already-manual", "no automatic triggers", triggers
        )

    forced = policy.matches_force_gate(ident)
    if forced:
        return Decision(repo, workflow, "gate", f"force_gate `{forced}`", triggers)

    kept = policy.matches_keep(ident)
    if kept:
        return Decision(repo, workflow, "keep", f"keep `{kept}`", triggers)

    return Decision(repo, workflow, "gate", "billable triggers", triggers)


def looks_like_ship_path(workflow: str) -> bool:
    """Heuristic used to seed a policy file, never to make the final call."""
    name = workflow.lower()
    return any(hint in name for hint in SHIP_HINTS)
