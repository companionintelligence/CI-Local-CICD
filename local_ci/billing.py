"""Decide whether a workflow actually costs money, and how much per minute.

Two things have to be true before GitHub charges for a run, and the rest of
this package only ever checked the first:

1. Something automatic has to start it. A ``workflow_dispatch``-only workflow
   sits there for free.
2. The minutes have to be billable minutes. GitHub-hosted runners on a *public*
   repo are free at standard sizes; self-hosted runners are free everywhere
   (you already paid for the hardware). Only hosted minutes on a private repo -
   or a larger runner anywhere - land on the invoice.

The per-minute rate is set by the runner OS, not by the trigger:
Linux 1x, Windows 2x, macOS 10x. A nightly macOS build is ten nightly Linux
builds, and a report that counts workflows rather than weighting them will rank
the bill wrong.

This module is deliberately free of any notion of gating. Gating is a switch we
control; billability is a property of the workflow itself. Conflating the two
is what produced a baseline full of ``billing: []`` for repos that were merely
switched off, and a budget decision made on the belief they were free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence

from .policy import MANUAL_TRIGGERS
from .workflows import PRESERVED_TRIGGERS

LINUX = "linux"
WINDOWS = "windows"
MACOS = "macos"
SELF_HOSTED = "self-hosted"
UNKNOWN = "unknown"

# Per-minute multipliers relative to Linux, per GitHub's published rates.
MULTIPLIERS = {LINUX: 1, WINDOWS: 2, MACOS: 10}

# USD per minute for a standard 2-core Linux runner; the others are derived.
LINUX_RATE_PER_MINUTE = 0.008

# Standard GitHub-hosted image labels. Anything else that is not self-hosted is
# either a larger runner or an org-defined label - both of which can bill on a
# public repo, so they must not fall into the "public repos are free" branch.
_STANDARD = {
    LINUX: {
        "ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04", "ubuntu-20.04",
        "ubuntu-24.04-arm", "ubuntu-22.04-arm",
    },
    WINDOWS: {
        "windows-latest", "windows-2025", "windows-2022", "windows-2019",
        "windows-11-arm",
    },
    MACOS: {
        "macos-latest", "macos-15", "macos-14", "macos-13",
    },
}

_PREFIX = ((LINUX, ("ubuntu", "linux")),
           (WINDOWS, ("windows",)),
           (MACOS, ("macos", "mac-os")))


def runner_platform(label: str) -> str:
    """Map one ``runs-on`` label to a billing platform."""
    value = label.strip().strip("\"'").lower()
    if not value or "${{" in value:
        return UNKNOWN
    if value == SELF_HOSTED or value.startswith("self-hosted"):
        return SELF_HOSTED
    for platform, prefixes in _PREFIX:
        if value.startswith(prefixes):
            return platform
    return UNKNOWN


def is_standard_runner(label: str) -> bool:
    """True for the free-on-public-repos GitHub-hosted images."""
    value = label.strip().strip("\"'").lower()
    return any(value in names for names in _STANDARD.values())


@dataclass
class BillingVerdict:
    """Why a workflow does or does not cost money."""

    billable: bool
    reason: str
    triggers: List[str] = field(default_factory=list)
    platforms: List[str] = field(default_factory=list)
    multiplier: int = 0

    @property
    def rate_per_minute(self) -> float:
        return round(self.multiplier * LINUX_RATE_PER_MINUTE, 4)


def automatic_triggers(triggers: Sequence[str]) -> List[str]:
    """Triggers that start a run without a human asking."""
    return [
        t for t in triggers
        if t not in MANUAL_TRIGGERS and t not in PRESERVED_TRIGGERS
    ]


def _as_groups(runners: Sequence) -> List[List[str]]:
    """Accept either ``["ubuntu-latest", ...]`` or ``[["self-hosted", ...], ...]``.

    A bare string is its own group; a nested sequence is a label conjunction
    that selects a single machine.
    """
    groups: List[List[str]] = []
    for entry in runners:
        if isinstance(entry, str):
            groups.append([entry])
        else:
            group = [str(x) for x in entry]
            if group:
                groups.append(group)
    return groups


def group_platform(group: Sequence[str]) -> str:
    """The billing platform of one ``runs-on`` label conjunction.

    ``self-hosted`` anywhere in the group wins: the job lands on our hardware
    regardless of what the other labels are named.
    """
    platforms = [runner_platform(label) for label in group]
    if SELF_HOSTED in platforms:
        return SELF_HOSTED
    for platform in platforms:
        if platform in MULTIPLIERS:
            return platform
    return UNKNOWN


def classify_billing(
    triggers: Sequence[str],
    runners: Sequence = (),
    private: bool = True,
) -> BillingVerdict:
    """Would this workflow bill, given its triggers and runners?

    ``private`` defaults to True because that is the safe direction: assume a
    workflow costs money until something proves it does not. Being wrong that
    way over-reports the bill; being wrong the other way is what got us here.
    """
    automatic = automatic_triggers(triggers)
    groups = _as_groups(runners)

    platforms: List[str] = []
    for group in groups:
        platform = group_platform(group)
        if platform not in platforms:
            platforms.append(platform)

    if not automatic:
        return BillingVerdict(False, "no automatic triggers", [], platforms, 0)

    hosted = [p for p in platforms if p != SELF_HOSTED]
    if groups and not hosted:
        return BillingVerdict(
            False, "self-hosted runners only", automatic, platforms, 0
        )

    known = [p for p in hosted if p in MULTIPLIERS]
    multiplier = max((MULTIPLIERS[p] for p in known), default=1)

    hosted_groups = [g for g in groups if group_platform(g) != SELF_HOSTED]
    if (not private and hosted_groups
            and all(len(g) == 1 and is_standard_runner(g[0])
                    for g in hosted_groups)):
        return BillingVerdict(
            False, "public repo on standard runners", automatic, platforms, 0
        )

    if not groups:
        # No `runs-on` anywhere: usually a caller that only `uses:` a reusable
        # workflow. The minutes are real, they are just billed against the
        # callee's runners, so report billable and say the size is unknown.
        return BillingVerdict(
            True, "automatic triggers, runner unknown", automatic, platforms, 1
        )

    if len(known) < len(hosted):
        return BillingVerdict(
            True, "automatic triggers, non-standard runner", automatic,
            platforms, multiplier,
        )

    worst = max(known, key=lambda p: MULTIPLIERS[p])
    return BillingVerdict(
        True, f"automatic triggers on {worst} ({multiplier}x)", automatic,
        platforms, multiplier,
    )
