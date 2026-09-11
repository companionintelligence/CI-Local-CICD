"""Read and rewrite the trigger (``on:``) block of a GitHub Actions workflow.

Everything here is deliberately line-surgery rather than a YAML round-trip. A
round-trip through PyYAML or ruamel reflows the entire file, which turns a
one-block change into an unreviewable diff across dozens of repos. We only
touch the lines that make up the ``on:`` block and leave every other byte of
the file exactly as it was.

Gating rewrites::

    on:
      push:
        branches: [main]
      pull_request:

into::

    # >>> ci-local:gated - original triggers below, restore with `ci-local restore`
    # on:
    #   push:
    #     branches: [main]
    #   pull_request:
    # <<< ci-local:gated
    on:
      workflow_dispatch:

The original is carried in the file itself, so ``restore`` needs no sidecar
state and the change is legible in review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

BEGIN_MARKER = "# >>> ci-local:gated"
END_MARKER = "# <<< ci-local:gated"
MARKER_NOTE = "original triggers below, restore with `ci-local restore`"

# A top-level `on:` key. YAML 1.1 folds bare `on` to boolean true, so authors
# also write it quoted; accept all three spellings.
_ON_KEY = re.compile(r"^(?P<key>on|\"on\"|'on')\s*:(?P<rest>.*)$")
# A top-level key that is not `on` - used to find where the on-block ends.
_TOP_LEVEL = re.compile(r"^[^\s#]")
_CHILD_KEY = re.compile(r"^(?P<indent>\s*)(?P<name>[A-Za-z_][\w-]*)\s*:")

# Triggers that must survive gating: removing them breaks other workflows that
# call this one, rather than merely saving minutes.
PRESERVED_TRIGGERS = ("workflow_call",)


class WorkflowParseError(ValueError):
    """The workflow has no usable top-level ``on:`` block."""


@dataclass
class OnBlock:
    """The ``on:`` block located inside a workflow file."""

    start: int  # first line index (inclusive)
    end: int  # last line index (exclusive)
    lines: List[str]
    inline: bool  # True for `on: push` / `on: [push, pull_request]`

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def read_workflow(path) -> str:
    """Read a workflow verbatim.

    ``Path.read_text`` applies universal-newline translation, which rewrites a
    CRLF file to LF on the way in. Writing it back then reformats every line of
    the file - a one-block edit turns into a whole-file diff. ``newline=""``
    disables the translation in both directions.
    """
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_workflow(path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def split_lines(text: str) -> Tuple[List[str], str]:
    """Split into lines, remembering whether the file ended with a newline."""
    ending = "\n" if text.endswith("\n") else ""
    body = text[:-1] if ending else text
    return body.split("\n"), ending


def find_on_block(text: str) -> OnBlock:
    """Locate the top-level ``on:`` block.

    Raises WorkflowParseError when the file has no top-level ``on:`` key, which
    is the case for workflow fragments and for composite actions.
    """
    lines, _ = split_lines(text)
    start = None
    inline = False
    for i, line in enumerate(lines):
        match = _ON_KEY.match(line)
        if match:
            start = i
            inline = bool(match.group("rest").strip())
            break
    if start is None:
        raise WorkflowParseError("no top-level `on:` key")

    if inline:
        # `on: push` or `on: [push, pull_request]` - a single line, unless the
        # flow sequence is wrapped across lines.
        end = start + 1
        if lines[start].count("[") > lines[start].count("]"):
            while end < len(lines) and lines[end].count("]") <= lines[end].count("["):
                end += 1
            end = min(end + 1, len(lines))
        return OnBlock(start, end, lines[start:end], inline=True)

    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and _TOP_LEVEL.match(line):
            break
        end += 1

    # Blank lines and column-0 comments trailing the block belong to whatever
    # comes next, not to `on:`.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return OnBlock(start, end, lines[start:end], inline=False)


def child_entries(block: OnBlock) -> Dict[str, List[str]]:
    """Map each direct child key of a mapping-style ``on:`` block to its lines.

    Returns an empty dict for inline or sequence-style blocks, which carry no
    per-trigger configuration worth preserving.
    """
    if block.inline or len(block.lines) < 2:
        return {}

    body = block.lines[1:]
    indent = None
    for line in body:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            return {}  # block sequence: `on:\n  - push`
        indent = len(line) - len(line.lstrip())
        break
    if indent is None:
        return {}

    entries: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in body:
        match = _CHILD_KEY.match(line)
        if match and len(match.group("indent")) == indent:
            current = match.group("name")
            entries[current] = [line]
        elif current is not None:
            entries[current].append(line)
    # Trailing blank lines inside an entry are noise.
    for key, value in entries.items():
        while value and not value[-1].strip():
            value.pop()
    return entries


def declared_triggers(text: str) -> List[str]:
    """Names of the triggers a workflow declares, best effort."""
    try:
        block = find_on_block(text)
    except WorkflowParseError:
        return []
    entries = child_entries(block)
    if entries:
        return list(entries)

    if block.inline:
        rest = _ON_KEY.match(block.lines[0]).group("rest")
        raw = " ".join([rest] + block.lines[1:])
        return [t for t in re.findall(r"[A-Za-z_][\w-]*", raw)]
    return [
        line.strip()[2:].strip()
        for line in block.lines[1:]
        if line.strip().startswith("- ")
    ]


# `runs-on:` decides the price of a minute, so it has to be read even out of a
# gated file. Matches the scalar and inline-sequence forms; the block-sequence
# form is picked up by the continuation scan below.
_RUNS_ON = re.compile(r"^\s*runs-on\s*:\s*(?P<rest>.*)$")
_LABEL = re.compile(r"[A-Za-z0-9_.\-]+")


def _strip_comment(value: str) -> str:
    """Drop a trailing ``# ...`` comment from a scalar YAML value."""
    if "${{" in value:  # expressions can legitimately contain '#'
        return value.strip()
    head = value.split(" #", 1)[0]
    return head.strip().strip("\"'")


def runner_targets(text: str) -> List[List[str]]:
    """One label group per ``runs-on:`` in the file.

    Grouping is the whole point. ``runs-on: [self-hosted, macos-test]`` is a
    *conjunction* - it selects one self-hosted machine carrying both labels, and
    costs nothing. Flattening it into two labels invents a hosted macOS runner
    and a 10x charge that does not exist.
    """
    lines, _ = split_lines(text)
    targets: List[List[str]] = []

    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        match = _RUNS_ON.match(line)
        if not match:
            continue
        rest = match.group("rest").strip()
        if "${{" in rest:
            targets.append([_strip_comment(rest)])
            continue
        if rest.startswith("["):
            group = [l for l in (_strip_comment(x) for x in _LABEL.findall(rest)) if l]
            if group:
                targets.append(group)
            continue
        if rest:
            label = _strip_comment(rest)
            if label:
                targets.append([label])
            continue
        # Block sequence:  runs-on:\n  - self-hosted\n  - linux
        indent = len(line) - len(line.lstrip())
        group = []
        for follow in lines[i + 1 :]:
            if not follow.strip() or follow.lstrip().startswith("#"):
                continue
            if len(follow) - len(follow.lstrip()) <= indent:
                break
            if not follow.lstrip().startswith("- "):
                break
            label = _strip_comment(follow.lstrip()[2:])
            if label:
                group.append(label)
        if group:
            targets.append(group)
    return targets


def runner_labels(text: str) -> List[str]:
    """Every ``runs-on:`` label in the file, flattened and deduplicated.

    For display only - billing must use :func:`runner_targets`, which keeps the
    grouping that says whether a job is self-hosted.
    """
    out: List[str] = []
    for group in runner_targets(text):
        for label in group:
            if label not in out:
                out.append(label)
    return out


def is_gated(text: str) -> bool:
    return BEGIN_MARKER in text and END_MARKER in text


def _marker_span(lines: List[str]) -> Optional[Tuple[int, int]]:
    """Indices of the gating markers: ``(begin, end)``, both inclusive.

    Returns None when the file is not gated, or when the markers are present
    but out of order - a half-applied edit should not be read as a gate.
    """
    begin = end = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if begin is None and stripped.startswith(BEGIN_MARKER):
            begin = i
        elif begin is not None and stripped.startswith(END_MARKER):
            end = i
            break
    if begin is None or end is None:
        return None
    return begin, end


def _uncomment(lines: List[str]) -> List[str]:
    """Strip the one-space comment prefix :func:`gate` added."""
    out = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("# "):
            out.append(stripped[2:])
        elif stripped.startswith("#"):
            out.append(stripped[1:])
        else:
            out.append(line)
    return out


def original_on_block(text: str) -> Optional[str]:
    """The pre-gating ``on:`` block, recovered from the marker comments.

    :func:`gate` rewrites the live ``on:`` block down to ``workflow_dispatch``
    and parks the real triggers in a comment block. Anything that asks a gated
    file what it runs on therefore gets ``["workflow_dispatch"]`` - which reads
    as "this workflow is free" when it actually means "we switched it off".
    This reads the comment block back so the original answer stays available.

    Returns None when the file is not gated.
    """
    if not is_gated(text):
        return None
    lines, _ = split_lines(text)
    span = _marker_span(lines)
    if span is None:
        return None
    begin, end = span
    return "\n".join(_uncomment(lines[begin + 1 : end]))


def restored_triggers(text: str) -> List[str]:
    """Triggers this workflow would fire on if it were restored.

    For an ungated workflow this is just :func:`declared_triggers`. For a gated
    one it comes from the commented original, so gating cannot erase a
    workflow's billing classification - the question "would restoring this cost
    money?" stays answerable after the switch is off.
    """
    original = original_on_block(text)
    if original is None:
        return declared_triggers(text)
    # Fall back to the live block if the comment block is unparseable, so a
    # hand-edited marker degrades to the old answer rather than to silence.
    return declared_triggers(original) or declared_triggers(text)


def gate(text: str) -> Tuple[str, bool]:
    """Rewrite a workflow so it only runs on demand.

    Returns ``(new_text, changed)``. Already-gated files, and files whose only
    triggers are manual or preserved, come back unchanged.
    """
    if is_gated(text):
        return text, False

    block = find_on_block(text)
    entries = child_entries(block)
    triggers = declared_triggers(text)

    keep = [t for t in triggers if t in PRESERVED_TRIGGERS]
    droppable = [
        t for t in triggers if t not in PRESERVED_TRIGGERS and t != "workflow_dispatch"
    ]
    if not droppable:
        return text, False  # already manual-only (plus anything preserved)

    lines, ending = split_lines(text)
    replacement = [f"{BEGIN_MARKER} {MARKER_NOTE}"]
    for line in block.lines:
        # Keep non-empty lines byte-exact so restore round-trips; an empty line
        # would otherwise become "# " and come back with phantom whitespace.
        replacement.append(f"# {line}" if line else "#")
    replacement.append(END_MARKER)
    replacement.append("on:")

    # Carry `workflow_dispatch` inputs across so a manual run keeps its form.
    if "workflow_dispatch" in entries:
        replacement.extend(entries["workflow_dispatch"])
    else:
        replacement.append("  workflow_dispatch:")
    for name in keep:
        if name in entries:
            replacement.extend(entries[name])
        else:
            replacement.append(f"  {name}:")

    new_lines = lines[: block.start] + replacement + lines[block.end :]
    return "\n".join(new_lines) + ending, True


def restore(text: str) -> Tuple[str, bool]:
    """Undo :func:`gate`, putting the original trigger block back."""
    if not is_gated(text):
        return text, False

    lines, ending = split_lines(text)
    span = _marker_span(lines)
    if span is None:
        # `is_gated` matches the marker anywhere, including mid-line; without
        # a real marker pair there is nothing to restore.
        return text, False
    begin, end = span

    original = _uncomment(lines[begin + 1 : end])

    # Drop the generated `on:` block that follows the marker. Slice the real
    # line list, never a re-split of the joined tail: joining swallows a
    # trailing empty line, and re-splitting would silently drop it from the
    # file. The block always precedes the tail, so the indices still line up.
    tail_lines = lines[end + 1 :]
    try:
        generated = find_on_block("\n".join(tail_lines))
    except WorkflowParseError:
        return text, False
    remainder = tail_lines[: generated.start] + tail_lines[generated.end :]

    new_lines = lines[:begin] + original + remainder
    return "\n".join(new_lines) + ending, True
