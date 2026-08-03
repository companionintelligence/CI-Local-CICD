"""Cap artifact retention on ``actions/upload-artifact`` steps.

Actions *storage* is billed no matter where the job ran, so gating workflows to
manual does nothing for it. In this workspace it is $38.56/month, and $37.70 of
that is CI-Hub - seven desktop-bundle uploads in ``desktop-build.yml`` with no
``retention-days`` at all, which means they inherit the 90-day default.

Same line-surgery discipline as :mod:`local_ci.workflows`: touch only the lines
that need changing so the diff stays reviewable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .workflows import split_lines

DEFAULT_RETENTION_DAYS = 7

_UPLOAD = re.compile(r"^(?P<indent>\s*)(?:-\s+)?uses:\s*actions/upload-artifact")
_KEY = re.compile(r"^(?P<indent>\s*)(?:-\s+)?(?P<name>[A-Za-z_][\w-]*)\s*:")
_RETENTION = re.compile(r"^(?P<indent>\s*)retention-days:\s*(?P<value>\d+)")


@dataclass
class Change:
    line: int
    action: str  # "lowered" | "added" | "added-with"
    detail: str


def _key_indent(line: str) -> int:
    """Column of the step's key, accounting for a leading ``- ``."""
    stripped = line.lstrip()
    indent = len(line) - len(stripped)
    return indent + 2 if stripped.startswith("- ") else indent


def _step_end(lines: List[str], start: int, key_indent: int) -> int:
    """Index just past the last line belonging to this step."""
    i = start + 1
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        # A new list item at or above this level, or any dedent, ends the step.
        if indent < key_indent or (indent == key_indent and stripped.startswith("- ")):
            break
        i += 1
    # Trailing blank lines belong to whatever follows.
    while i > start + 1 and not lines[i - 1].strip():
        i -= 1
    return i


def apply_retention(
    text: str, days: int = DEFAULT_RETENTION_DAYS
) -> Tuple[str, List[Change]]:
    """Ensure every upload-artifact step retains for at most ``days``.

    Existing values at or below the cap are left alone - lowering someone's
    deliberate 3-day setting to 7 would be an increase, not a saving.
    """
    lines, ending = split_lines(text)
    changes: List[Change] = []
    out = list(lines)
    offset = 0

    for index, raw in enumerate(lines):
        if not _UPLOAD.match(raw):
            continue
        key_indent = _key_indent(raw)
        end = _step_end(lines, index, key_indent)

        with_at: Optional[int] = None
        retention_at: Optional[int] = None
        for i in range(index + 1, end):
            match = _KEY.match(lines[i])
            if match and len(match.group("indent")) == key_indent:
                if match.group("name") == "with":
                    with_at = i
            hit = _RETENTION.match(lines[i])
            if hit and len(hit.group("indent")) > key_indent:
                retention_at = i

        if retention_at is not None:
            current = int(_RETENTION.match(lines[retention_at]).group("value"))
            if current <= days:
                continue
            indent = _RETENTION.match(lines[retention_at]).group("indent")
            out[retention_at + offset] = f"{indent}retention-days: {days}"
            changes.append(
                Change(retention_at + 1, "lowered", f"{current} -> {days} days")
            )
            continue

        if with_at is not None:
            # Match the indentation of the first existing `with:` child.
            child_indent = " " * (key_indent + 2)
            for i in range(with_at + 1, end):
                if lines[i].strip():
                    child_indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                    break
            out.insert(with_at + 1 + offset, f"{child_indent}retention-days: {days}")
            offset += 1
            changes.append(Change(with_at + 2, "added", f"{days} days"))
        else:
            pad = " " * key_indent
            out.insert(end + offset, f"{pad}with:")
            out.insert(end + 1 + offset, f"{pad}  retention-days: {days}")
            offset += 2
            changes.append(Change(end + 1, "added-with", f"{days} days"))

    if not changes:
        return text, []
    return "\n".join(out) + ending, changes
