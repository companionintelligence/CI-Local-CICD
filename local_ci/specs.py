"""The spec slot: one check per entry in a repo's spec registry.

A repo lists the formal and executable specifications it keeps in
``.agent-configs/specs.toml``. Each entry that has a ``check`` and is not
``planned`` becomes a check named ``spec:<id>``. It runs without a shell from
the repo root, or from the entry's ``workdir``, for at most ``timeout_seconds``
(300 by default). A ``required`` entry fails the run like any other check. An
``advisory`` entry, which every ``draft`` is, is reported as a warning and
never fails it. ``--only spec`` selects the whole slot.

CI-Agent-Sync owns the format and validates all of it. This module checks only
what ci-local needs to run something: ``version``, and each entry's ``id``,
``status``, ``gate``, ``check``, ``workdir`` and ``timeout_seconds``. A registry
that fails those checks is not skipped. It becomes one failing check named
``spec``, so a typo cannot quietly switch a gate off.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .detect import Check, RepoPlan

REGISTRY_FILE = ".agent-configs/specs.toml"

# `--only spec` selects `spec:<id>` for every entry, and `spec` itself, which
# is the check an invalid registry becomes.
SLOT = "spec"
PREFIX = f"{SLOT}:"

DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 86_400

STATUSES = ("planned", "draft", "checked", "conformance")
GATES = ("advisory", "required")
# A required gate needs a check that already passes on the default branch.
GATING_STATUSES = ("checked", "conformance")

KEBAB_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


@dataclass
class Spec:
    """The part of a registry entry that ci-local runs."""

    id: str
    status: str
    gate: str = "advisory"
    check: List[str] = field(default_factory=list)
    workdir: Optional[str] = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


@dataclass
class Registry:
    specs: List[Spec] = field(default_factory=list)
    # Each one `<where>: <what>`, e.g. `spec[1].gate: ...`.
    problems: List[str] = field(default_factory=list)


def in_slot(name: str) -> bool:
    return name == SLOT or name.startswith(PREFIX)


def load(root: Path) -> Optional[Registry]:
    """The registry in the checkout at ``root``, or None when there is none."""
    path = root / REGISTRY_FILE
    if not os.path.lexists(path):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return Registry(problems=["not UTF-8 text"])
    except OSError as exc:
        # The reason alone: the path would put this machine's layout into
        # generated docs.
        return Registry(problems=[f"cannot be read: {exc.strerror or type(exc).__name__}"])
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return Registry(problems=[f"not valid TOML: {exc}"])
    return parse(data)


def parse(data: Dict[str, Any]) -> Registry:
    """Check the fields ci-local uses, reporting every problem at once."""
    registry = Registry()
    problems = registry.problems

    if "version" not in data:
        problems.append("version: missing (this format is version 1)")
    elif not _is_int(data["version"]) or data["version"] != 1:
        problems.append(f"version: must be 1, not {_show(data['version'])}")

    entries = data.get("spec", [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        problems.append("spec: must be an array of tables, written [[spec]]")
        entries = []

    first_use: Dict[str, int] = {}
    for index, entry in enumerate(entries):
        spec = _entry(entry, f"spec[{index}]", problems)
        ident = entry.get("id")
        if isinstance(ident, str):
            if ident in first_use:
                problems.append(f'spec[{index}].id: duplicate id "{ident}": '
                                f"spec[{first_use[ident]}] already uses it")
            else:
                first_use[ident] = index
        if spec is not None:
            registry.specs.append(spec)
    return registry


def _entry(entry: Dict[str, Any], where: str, problems: List[str]) -> Optional[Spec]:
    before = len(problems)

    ident = entry.get("id")
    if ident is None:
        problems.append(f"{where}.id: missing")
    elif not isinstance(ident, str) or not KEBAB_ID.fullmatch(ident):
        problems.append(f"{where}.id: must be a kebab-case id such as "
                        f"action-ledger, not {_show(ident)}")

    status = entry.get("status")
    if status is None:
        problems.append(f"{where}.status: missing")
    elif status not in STATUSES:
        problems.append(f"{where}.status: must be one of {', '.join(STATUSES)}, "
                        f"not {_show(status)}")
        status = None

    gate = entry.get("gate", "advisory")
    if gate not in GATES:
        problems.append(f"{where}.gate: must be advisory or required, not {_show(gate)}")
    elif gate == "required" and status is not None and status not in GATING_STATUSES:
        problems.append(f'{where}.gate: "required" needs status "checked" or '
                        f'"conformance", not "{status}"')

    check = entry.get("check")
    if check is None:
        if status is not None and status != "planned":
            problems.append(f'{where}.check: required unless status is "planned"')
    elif not (isinstance(check, list) and check
              and all(isinstance(arg, str) and arg for arg in check)):
        problems.append(f"{where}.check: must be an argv array of non-empty "
                        f"strings, not {_show(check)}")

    workdir = entry.get("workdir")
    if workdir is not None:
        problem = _repo_path_problem(workdir)
        if problem:
            problems.append(f"{where}.workdir: {problem}")

    timeout = entry.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not _is_int(timeout) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        problems.append(f"{where}.timeout_seconds: must be a whole number from 1 "
                        f"to {MAX_TIMEOUT_SECONDS}, not {_show(timeout)}")

    if len(problems) > before:
        return None
    return Spec(ident, status, gate, list(check or []), workdir, timeout)


def _repo_path_problem(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return f"must be a non-empty path, not {_show(value)}"
    if value.startswith("/") or re.match(r"[A-Za-z]:", value):
        return "must be relative to the repository root"
    if "\\" in value:
        return "must use forward slashes"
    if ".." in value.split("/"):
        return "must not contain a '..' segment"
    return None


def _is_int(value: Any) -> bool:
    # TOML `true` arrives as a Python bool, which is also an int.
    return isinstance(value, int) and not isinstance(value, bool)


def _show(value: Any) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= 60 else text[:57] + "..."


def add_spec_checks(plan: RepoPlan, root: Path) -> None:
    """Append the slot to ``plan``, reading the registry under ``root``."""
    registry = load(root)
    if registry is None:
        return
    if registry.problems:
        plan.checks.append(Check(
            SLOT, [],
            error=f"{REGISTRY_FILE} is invalid: " + "; ".join(registry.problems),
        ))
        return

    planned = 0
    for spec in registry.specs:
        if spec.status == "planned":
            planned += 1
            continue
        required = spec.gate == "required"
        plan.checks.append(Check(
            PREFIX + spec.id,
            list(spec.check),
            required=required,
            advisory=not required,
            cwd=root / spec.workdir if spec.workdir else root,
            timeout=spec.timeout_seconds,
        ))
    if planned:
        plan.notes.append(f"{planned} planned spec(s) in {REGISTRY_FILE}, not run")
