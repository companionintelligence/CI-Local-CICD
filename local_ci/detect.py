"""Work out how to lint, test and build a repo on local hardware.

Detection is deliberately conservative: we only emit a check when there is
direct evidence it exists (a script in ``package.json``, a ``Cargo.toml``, and
so on). A missing check is reported as "not detected" rather than guessed at,
because a plan that invents commands produces failures nobody trusts.
"""

from __future__ import annotations

import json
import re
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
    # Nested checkouts found inside the repo; results are unreliable if set.
    contaminated: List[str] = field(default_factory=list)

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


# What each manager writes inside `node_modules` when it reconciles the tree
# with the lockfile; its mtime is when that last happened. bun has no such file
# worth relying on, so it falls back to the directory's own mtime.
NODE_INSTALL_MARKERS = {
    "npm": ".package-lock.json",
    "pnpm": ".modules.yaml",
    "yarn": ".yarn-state.yml",
}


def node_install_reason(path: Path, manager: str) -> Optional[str]:
    """Why the node tree has to be installed before checks run, or ``None``.

    `node_modules` existing is not the same as `node_modules` being current.
    A tree installed before a dependency bump keeps serving the superseded
    package - the top-level copy stays whatever got hoisted there at install
    time, even once the lockfile says otherwise, because nothing rewrites it
    until an install runs. Checks then run against versions no commit
    describes, and nothing in the output says so: a test fails, and the diff
    that "broke" it does not exist.

    Staleness is read from mtimes, comparing the lockfile against the marker
    the manager leaves in `node_modules`. That is a heuristic, so it is pointed
    the safe way - every install command here is lockfile-frozen, which makes a
    false positive cost one reinstall and a false negative cost an afternoon.
    """
    modules = path / "node_modules"
    if not modules.exists():
        return "node_modules missing"

    locks = [path / name for name, mgr in LOCKFILE_MANAGERS if mgr == manager]
    present = [f for f in locks if f.exists()]
    if not present:
        # No lockfile to be stale against - `install` would resolve fresh, so an
        # existing tree is as good an answer as any.
        return None

    marker_name = NODE_INSTALL_MARKERS.get(manager)
    marker = modules / marker_name if marker_name else None
    try:
        installed_at = (
            marker.stat().st_mtime
            if marker is not None and marker.exists()
            else modules.stat().st_mtime
        )
        newest = max(present, key=lambda f: f.stat().st_mtime)
        if newest.stat().st_mtime <= installed_at:
            return None
    except OSError:
        # Can't read the timestamps, so can't claim the tree is stale.
        return None
    return f"{newest.name} is newer than the installed tree"


def workspace_globs(pkg: Dict, path: Path) -> List[str]:
    """Workspace patterns from package.json, or pnpm-workspace.yaml."""
    raw = pkg.get("workspaces")
    if isinstance(raw, dict):
        raw = raw.get("packages")
    if isinstance(raw, list):
        return [str(p) for p in raw]

    pnpm = path / "pnpm-workspace.yaml"
    if pnpm.exists():
        globs = []
        try:
            in_packages = False
            for line in pnpm.read_text(encoding="utf-8").splitlines():
                if line.startswith("packages:"):
                    in_packages = True
                    continue
                if in_packages:
                    stripped = line.strip()
                    if stripped.startswith("- "):
                        globs.append(stripped[2:].strip().strip("\"'"))
                    elif stripped and not stripped.startswith("#"):
                        break
        except OSError:
            return []
        return globs
    return []


def workspace_checks(path: Path, pkg: Dict, manager: str) -> List[Check]:
    """Checks defined by workspace packages rather than the root manifest.

    A monorepo root often has only `lint`; the tests live in the packages. The
    root manifest alone reports "no test script", which reads as "this repo has
    no tests" - wrong, and exactly the case that made CI-Server's generated
    docs say lint-only for a repo that was the single biggest line on the bill.
    """
    globs = workspace_globs(pkg, path)
    if not globs:
        return []

    checks: List[Check] = []
    seen = set()
    for pattern in globs:
        for child in sorted(path.glob(pattern)):
            if not child.is_dir():
                continue
            child_pkg = _read_json(child / "package.json")
            if not child_pkg:
                continue
            name = child_pkg.get("name")
            scripts = child_pkg.get("scripts", {}) or {}
            if not name:
                continue
            for slot, aliases in NODE_CHECKS:
                if slot == "build":
                    continue  # building every package is a release concern
                for alias in aliases:
                    body = scripts.get(alias)
                    if not body or (slot == "test" and PLACEHOLDER_TEST in body):
                        continue
                    key = (name, slot)
                    if key in seen:
                        break
                    seen.add(key)
                    checks.append(Check(
                        f"{slot}:{name}",
                        _workspace_run(manager, str(name), alias),
                    ))
                    break
    return checks


def _workspace_run(manager: str, package: str, script: str) -> List[str]:
    if manager == "pnpm":
        return ["pnpm", "--filter", package, "run", script]
    if manager == "yarn":
        return ["yarn", "workspace", package, script]
    if manager == "bun":
        return ["bun", "run", "--filter", package, script]
    return ["npm", "run", "--silent", script, "--workspace", package]


def makefile_targets(path: Path) -> List[str]:
    """Target names declared in a Makefile, ignoring pattern and .PHONY lines."""
    makefile = path / "Makefile"
    if not makefile.exists():
        return []
    targets = []
    try:
        for line in makefile.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"^([a-zA-Z][\w-]*)\s*:(?!=)", line)
            if match:
                targets.append(match.group(1))
    except OSError:
        return []
    return targets


def nested_checkouts(path: Path, limit: int = 200) -> List[str]:
    """Checkouts living *inside* the repo, relative to it.

    Agent sessions create git worktrees under `.claude/worktrees/`, so a repo
    can contain dozens of parallel copies of itself. Every tree-walking tool -
    eslint, tsc, vitest, ripgrep - then silently multiplies its work by that
    count and reports findings for code the developer never wrote.

    Measured on CI-Server: 18 nested checkouts turned a clean tree into 19,096
    lint problems. Hosted CI never saw it, because a fresh clone has none of
    them. This is a property of how the workspace is driven, not of any repo,
    so it is detected here rather than patched into each repo's linter config.
    """
    found: List[str] = []
    for parent in (".claude/worktrees", ".worktrees"):
        root = path / parent
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if (child / ".git").exists():
                found.append(f"{parent}/{child.name}")
                if len(found) >= limit:
                    return found
    return found


def plan_for(repo: str, path: Path, override=None) -> RepoPlan:
    return _warn_nested(_plan_for(repo, path, override))


def _plan_for(repo: str, path: Path, override=None) -> RepoPlan:
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

        # Workspace packages carry the tests in a monorepo. Only look when the
        # root is missing a slot, so a root-level `test` that already fans out
        # is not duplicated per package.
        root_slots = {c.name for c in plan.checks}
        if "test" not in root_slots or "typecheck" not in root_slots:
            extra = [
                c for c in workspace_checks(path, pkg, manager)
                if c.name.split(":", 1)[0] not in root_slots
            ]
            if extra:
                plan.checks.extend(extra)
                packages = sorted({c.name.split(":", 1)[1] for c in extra})
                plan.notes = [n for n in plan.notes
                              if n not in ("no test script", "no typecheck script")]
                plan.notes.append(
                    f"{len(extra)} check(s) from {len(packages)} workspace package(s)"
                )

        reason = node_install_reason(path, manager)
        if reason:
            plan.notes.append(f"{reason} - install required")
        targets = makefile_targets(path)
        if targets:
            interesting = [t for t in ("build", "dev", "test", "up", "reset")
                           if t in targets]
            if interesting:
                plan.notes.append(
                    "Makefile also provides: " + ", ".join(f"make {t}" for t in interesting)
                )
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


def _warn_nested(plan: RepoPlan) -> RepoPlan:
    nested = nested_checkouts(plan.path)
    if nested:
        plan.contaminated = nested
        plan.notes.append(
            f"WARNING: {len(nested)} nested checkout(s) inside this repo "
            f"({nested[0]}...). Tree-walking checks will scan them too and "
            f"report findings for code you did not write."
        )
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
