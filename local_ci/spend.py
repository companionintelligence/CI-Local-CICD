"""The spend state machine: three independent axes, decided one at a time.

#9 fixed one conflation (cost vs gating) and left another standing. The report
it shipped asked ``status != "BILLING"`` and called everything left over
"dormant, only not billing because it is gated" - which swept up every *kept*
workflow, because ``status`` returns ``"kept"`` for those. CI-Portal's
``deploy.yml`` is kept, ungated, on ``push`` to main, on a hosted Linux runner
in a private repo, and it deployed to production at 2026-08-10T23:27:40Z. The
report called it dormant.

Three questions, three answers, no borrowing between them:

1. **Is anything automatic live right now?** Decided by the triggers on the
   default branch, not by the gating marker. The marker is evidence about why,
   not proof: a hand-edited file can carry the marker and a live ``push:``
   anyway, and in that case it is billing no matter what the comment claims.
2. **Would a run cost money?** :mod:`local_ci.billing`, from the restored
   triggers, never consulting gating.
3. **Has it run lately?** :mod:`local_ci.activity`, from run history.

The policy ``keep`` list is *not* a fourth axis. It records a decision we made
about a workflow; it changes nothing about whether that workflow spends money.
It travels through here as a label so reports can show it, and it is never read
by :func:`classify_spend`.

The activity axis stays out of the state name for the same reason. An idle
live-billing workflow is still live-billing - it bills the moment someone
pushes - it just has a less certain monthly figure. Folding "dormant" into the
state is precisely the mistake above, one axis wearing another's name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from .activity import UNKNOWN as ACTIVITY_UNKNOWN
from .activity import RunActivity
from .billing import BillingVerdict

# --- axis 1 x axis 2: the five spend states -------------------------------
LIVE_BILLING = "live-billing"    # automatic triggers live, and they cost money
LIVE_FREE = "live-free"          # automatic triggers live, on free minutes
GATED_BILLABLE = "gated-billable"  # switched off, and would cost money if restored
GATED_FREE = "gated-free"        # switched off, and free anyway
MANUAL = "manual"                # never gated, but only a human can start it

#: States that put minutes on the invoice today.
BILLING_NOW = (LIVE_BILLING,)
#: States where restoring the workflow would start spend. Not billing today.
RESTORE_COST = (GATED_BILLABLE,)

_EXPLAIN = {
    LIVE_BILLING: "automatic triggers are live and billable",
    LIVE_FREE: "automatic triggers are live but cost nothing",
    GATED_BILLABLE: "gated; would bill again if restored",
    GATED_FREE: "gated, and free even if restored",
    MANUAL: "no automatic triggers; runs only when asked",
}


@dataclass(frozen=True)
class SpendState:
    """Where one workflow sits on all three axes at once."""

    state: str
    triggers_live: bool
    billable: bool
    activity: str = ACTIVITY_UNKNOWN
    multiplier: int = 0
    reason: str = ""
    # Why nothing automatic is live / why we left it alone. Labels, not inputs.
    gated: bool = False
    kept: Optional[str] = None

    @property
    def is_billing_now(self) -> bool:
        """Costing money today. True for a kept workflow; kept is not free."""
        return self.state in BILLING_NOW

    @property
    def is_restore_cost(self) -> bool:
        """Costs nothing today *because it is gated*, and would if restored."""
        return self.state in RESTORE_COST

    def describe(self) -> str:
        note = _EXPLAIN[self.state]
        if self.kept:
            note += f"; kept by `{self.kept}`"
        return f"{self.state} ({note})"


def classify_spend(
    triggers_live: bool,
    verdict: BillingVerdict,
    gated: bool = False,
    activity: Optional[RunActivity] = None,
    kept: Optional[str] = None,
) -> SpendState:
    """Decide the spend state. ``kept`` is carried, never consulted.

    ``triggers_live`` is the live-automatic-trigger question already answered -
    pass ``bool(workflow.billable)``. It leads, and ``gated`` only explains the
    negative case, so a file whose marker and triggers disagree is classified by
    the triggers GitHub will actually evaluate.
    """
    activity_state = activity.state if activity is not None else ACTIVITY_UNKNOWN

    if triggers_live:
        state = LIVE_BILLING if verdict.billable else LIVE_FREE
    elif gated:
        state = GATED_BILLABLE if verdict.billable else GATED_FREE
    else:
        state = MANUAL

    return SpendState(
        state=state,
        triggers_live=triggers_live,
        billable=bool(verdict.billable),
        activity=activity_state,
        multiplier=verdict.multiplier,
        reason=verdict.reason,
        gated=gated,
        kept=kept,
    )


def linux_equivalent(states: Iterable[SpendState]) -> int:
    """Sum of runner multipliers - one macOS workflow counts as ten Linux ones.

    Counting workflows instead ranks the bill wrong, which is the same class of
    error as counting a live workflow as dormant: the unit is not the thing.
    """
    return sum(s.multiplier for s in states)


def group_by_state(states: Sequence[SpendState]) -> dict:
    out: dict = {}
    for s in states:
        out.setdefault(s.state, []).append(s)
    return out


def state_counts(states: Sequence[SpendState]) -> dict:
    return {name: len(items) for name, items in group_by_state(states).items()}


def states_in(states: Sequence[SpendState], names: Sequence[str]) -> List[SpendState]:
    return [s for s in states if s.state in names]
