"""Add a top-level ``concurrency`` block so superseded runs get cancelled.

Without one, pushing three commits to a branch runs the whole suite three
times and pays for all three. GitHub only cancels runs that share a
concurrency group.

The group must include the ref. ``${{ github.workflow }}`` alone would make
every open PR cancel every other one, which is worse than paying.

Cancellation is *not* universally safe. Killing a deploy or a release
mid-flight can leave a half-published artifact or a partially rolled-out
environment, so ship-path workflows get ``cancel-in-progress: false``: runs
queue instead of overlapping, which still prevents concurrent deploys without
ever interrupting one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .policy import looks_like_ship_path
from .workflows import find_on_block, split_lines, WorkflowParseError

GROUP = "${{ github.workflow }}-${{ github.ref }}"

# Top-level keys that conventionally sit above `jobs:`. The block goes after
# the trigger definition but before `jobs:`, matching how the repos that
# already have one are written.
_TOP_LEVEL = re.compile(r"^(jobs|permissions|env|defaults|concurrency)\s*:", re.M)


@dataclass
class ConcurrencyChange:
    line: int
    cancel: bool
    group: str


def has_concurrency(text: str) -> bool:
    return bool(re.search(r"^concurrency\s*:", text, re.M))


def _insert_at(lines: List[str], start: int) -> int:
    """First line at or after ``start`` that begins a new top-level key."""
    for i in range(start, len(lines)):
        line = lines[i]
        if not line.strip() or line.startswith((" ", "\t", "#")):
            continue
        if re.match(r"^[A-Za-z_'\"][\w\-'\"]*\s*:", line):
            return i
    return len(lines)


def add_concurrency(
    text: str,
    workflow_name: str = "",
    cancel: Optional[bool] = None,
    group: str = GROUP,
) -> Tuple[str, Optional[ConcurrencyChange]]:
    """Insert a concurrency block. Returns unchanged text if one exists.

    ``cancel`` defaults to False for anything that looks like a deploy or
    release path - see the module docstring.
    """
    if has_concurrency(text):
        return text, None
    if cancel is None:
        cancel = not looks_like_ship_path(workflow_name)

    lines, ending = split_lines(text)
    try:
        block = find_on_block(text)
    except WorkflowParseError:
        return text, None

    insert = _insert_at(lines, block.end)

    # Keep exactly one blank line either side so the file still reads well.
    chunk = [
        "concurrency:",
        f"  group: {group}",
        f"  cancel-in-progress: {'true' if cancel else 'false'}",
    ]
    before_blank = insert > 0 and not lines[insert - 1].strip()
    body = ([] if before_blank else [""]) + chunk + [""]

    # Avoid stacking a second blank line against one already there.
    if insert < len(lines) and not lines[insert].strip():
        body = body[:-1]

    out = lines[:insert] + body + lines[insert:]
    offset = 1 if before_blank else 2
    return "\n".join(out) + ending, ConcurrencyChange(
        insert + offset, cancel, group
    )
