"""Work out how to lint, test and build a repo on local hardware.

Detection is deliberately conservative: we only emit a check when there is
direct evidence it exists (a script in ``package.json``, a ``Cargo.toml``, and
so on). A missing check is reported as "not detected" rather than guessed at,
because a plan that invents commands produces failures nobody trusts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# Node scripts we care about, in the order CI would run them. Several spellings
# exist across these repos, so each slot lists its aliases.
NODE_CHECKS = [
    ("lint", ["lint", "lint:check", "eslint"]),
    ("typecheck", ["typecheck", "type-check", "tsc", "check-types"]),
    ("test", ["test:ci", "test:unit", "test"]),
    ("build", ["build"]),
]

# `npm init` leaves a placeholder test script behind; running it proves nothing.
PLACEHOLDER_TEST = "no test specified"

LOCKFILE_MANAGERS = [
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("package-lock.json", "npm"),
]


@dataclass
class Check:
    """One command to run, and whether its failure should fail the repo."""

    name: str
    argv: List[str]
    required: bool = True

    @property
    def display(self) -> str:
        return " ".join(self.argv)


@dataclass
class RepoPlan:
    repo: str
    path: Path
    stack: str
    manager: Optional[str] = None
    checks: List[Check] = field(default_factory=list)
    install: Optional[Check] = None
    notes: List[str] = field(default_factory=list)

    @property
    def runnable(self) -> bool:
        return bool(self.checks)


def _read_json(path: Path) -> Dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def detect_node_manager(path: Path, pkg: Dict) -> str:
    """Lockfile wins over the ``packageManager`` field, which is often stale."""
    for lockfile, manager in LOCKFILE_MANAGERS:
        if (path / lockfile).exists():
            return manager
    declared = str(pkg.get("packageManager", ""))
    for manager in ("pnpm", "yarn", "bun", "npm"):
        if declared.startswith(manager):
            return manager
    return "npm"


def _node_run(manager: str, script: str) -> List[str]:
    # npm needs the `run` verb and `--` to pass through; the others take the
    # script name directly.
    if manager == "npm":
        return ["npm", "run", "--silent", script]
    return [manager, "run", script]


def _node_install(manager: str) -> List[str]:
    return {
        "pnpm": ["pnpm", "install", "--frozen-lockfile"],
        "yarn": ["yarn", "install", "--immutable"],
        "bun": ["bun", "install", "--frozen-lockfile"],
        "npm": ["npm", "ci"],
    }[manager]


def plan_for(repo: str, path: Path, override=None) -> RepoPlan:
    """Build the local check plan for a single repo.

    ``override`` is a :class:`local_ci.policy.RepoOverride`, applied before
    detection so that a repo whose project lives in a subdirectory is detected
    there rather than at a decoy root manifest.
    """
    if override is not None:
        if override.skip:
            plan = RepoPlan(repo, path, stack="skipped")
            plan.notes.append("skipped by policy")
            return plan
        if override.workdir:
            resolved = path / override.workdir
            if not resolved.is_dir():
                plan = RepoPlan(repo, path, stack="unknown")
                plan.notes.append(f"configured workdir `{override.workdir}` missing")
                return plan
            path = resolved
        if override.commands:
            plan = RepoPlan(repo, path, stack="custom")
            plan.checks = [
                Check(
                    str(c.get("name", "check")),
                    list(c.get("run", [])),
                    required=bool(c.get("required", True)),
                )
                for c in override.commands
            ]
            plan.notes.append("commands set by policy")
            return plan

    pkg_path = path / "package.json"
    if pkg_path.exists():
        pkg = _read_json(pkg_path)
        scripts = pkg.get("scripts", {}) or {}
        manager = detect_node_manager(path, pkg)
        plan = RepoPlan(repo, path, stack="node", manager=manager)
        plan.install = Check("install", _node_install(manager), required=True)

        for slot, aliases in NODE_CHECKS:
            for alias in aliases:
                body = scripts.get(alias)
                if not body:
                    continue
                if slot == "test" and PLACEHOLDER_TEST in body:
                    plan.notes.append("test script is an npm placeholder - skipped")
                    break
                plan.checks.append(Check(slot, _node_run(manager, alias)))
                break
            else:
                plan.notes.append(f"no {slot} script")
        if not (path / "node_modules").exists():
            plan.notes.append("node_modules missing - install required")
        return plan

    if (path / "Cargo.toml").exists():
        plan = RepoPlan(repo, path, stack="rust", manager="cargo")
        plan.checks = [
            Check("lint", ["cargo", "fmt", "--all", "--check"], required=False),
            Check("clippy", ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"]),
            Check("test", ["cargo", "test", "--all"]),
            Check("build", ["cargo", "build", "--release"], required=False),
        ]
        return plan

    if (path / "pubspec.yaml").exists():
        plan = RepoPlan(repo, path, stack="flutter", manager="flutter")
        plan.install = Check("install", ["flutter", "pub", "get"])
        plan.checks = [
            Check("lint", ["flutter", "analyze"]),
            Check("test", ["flutter", "test"]),
        ]
        return plan

    if (path / "go.mod").exists():
        plan = RepoPlan(repo, path, stack="go", manager="go")
        plan.checks = [
            Check("lint", ["go", "vet", "./..."]),
            Check("test", ["go", "test", "./..."]),
            Check("build", ["go", "build", "./..."], required=False),
        ]
        return plan

    if (path / "pyproject.toml").exists() or (path / "requirements.txt").exists():
        plan = RepoPlan(repo, path, stack="python", manager="python")
        plan.checks = [
            Check("lint", ["ruff", "check", "."], required=False),
            Check("test", ["pytest", "-q"]),
        ]
        return plan

    plan = RepoPlan(repo, path, stack="unknown")
    plan.notes.append("no recognised project manifest")
    return plan


def discover_repos(workspace: Path, only: Optional[List[str]] = None) -> List[Path]:
    """Every immediate subdirectory of the workspace that is a git repo."""
    repos = []
    for child in sorted(workspace.iterdir()):
        if not child.is_dir() or not (child / ".git").exists():
            continue
        if only and child.name not in only:
            continue
        repos.append(child)
    return repos
