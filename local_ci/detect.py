"""Work out how to lint, test and build a repo on local hardware.

Detection is deliberately conservative: we only emit a check when there is
direct evidence it exists (a script in ``package.json``, a ``Cargo.toml``, and
so on). A missing check is reported as "not detected" rather than guessed at,
because a plan that invents commands produces failures nobody trusts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
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

MANAGER_LOCKFILES: Dict[str, List[str]] = {}
for _lockfile, _manager in LOCKFILE_MANAGERS:
    MANAGER_LOCKFILES.setdefault(_manager, []).append(_lockfile)


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


def _is_git_tracked(path: Path, filename: str) -> bool:
    """Whether ``filename`` is committed in ``path``'s repo, not just present on disk.

    A package manager can leave a lockfile on disk that git never tracked, or no
    longer tracks — gitignored by policy (a repo that installs from the manifest
    alone), mid-migration to a different manager, or just a stale local `install`
    predating the manifest's current dependencies. `Path.exists()` cannot tell
    that apart from a committed lockfile, so a frozen install (`npm ci` and
    friends) must not trust one without checking.

    When ``path`` is not inside a git repo at all, there is nothing to check
    against, so this assumes tracked and leaves behaviour unchanged — every real
    caller reaches this via `discover_repos` or `resolve_checkout`, which both
    require a repo, so that fallback only matters for bare test fixtures.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", filename],
            cwd=path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=git_env(),
        )
    except OSError:
        return True
    if result.returncode == 0:
        return True
    return b"not a git repository" in result.stderr


def _has_tracked_lockfile(path: Path, manager: str) -> bool:
    return any(
        (path / lockfile).exists() and _is_git_tracked(path, lockfile)
        for lockfile in MANAGER_LOCKFILES.get(manager, [])
    )


def detect_node_manager(path: Path, pkg: Dict) -> str:
    """Lockfile wins over the ``packageManager`` field, which is often stale.

    Only a lockfile git actually tracks counts as a signal — see
    :func:`_is_git_tracked` for why an untracked one on disk cannot be trusted.
    """
    for lockfile, manager in LOCKFILE_MANAGERS:
        if (path / lockfile).exists() and _is_git_tracked(path, lockfile):
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


def _node_install(manager: str, frozen: bool) -> List[str]:
    """The install command for ``manager``.

    The frozen forms (`npm ci`, `--immutable`, `--frozen-lockfile`) require an
    exact, trustworthy lockfile and fail loudly when the manifest has moved on
    without it — exactly what you want when git tracks that lockfile as part of
    the repo's contract. When it does not (see :func:`_has_tracked_lockfile`),
    the same strictness just means every install command fails the moment
    anyone touches a dependency, so this falls back to a plain install.
    """
    if frozen:
        return {
            "pnpm": ["pnpm", "install", "--frozen-lockfile"],
            "yarn": ["yarn", "install", "--immutable"],
            "bun": ["bun", "install", "--frozen-lockfile"],
            "npm": ["npm", "ci"],
        }[manager]
    return {
        "pnpm": ["pnpm", "install"],
        "yarn": ["yarn", "install"],
        "bun": ["bun", "install"],
        "npm": ["npm", "install"],
    }[manager]


# Files a package manager rewrites on every successful install. If the manifest
# or a lockfile is newer than all of them, the tree on disk predates the current
# dependency set.
INSTALL_MARKERS = {
    "pnpm": ["node_modules/.modules.yaml"],
    "npm": ["node_modules/.package-lock.json"],
    "yarn": [".yarn/install-state.gz", "node_modules/.yarn-integrity"],
    "bun": ["node_modules/.bun-tag", "node_modules/.package-lock.json"],
}

# Whose mtime marks a change to the dependency set.
INSTALL_INPUTS = ["package.json"] + [name for name, _ in LOCKFILE_MANAGERS]


def _newest(path: Path, names: List[str]):
    """(mtime, name) of the most recently touched of ``names``, or (None, "")."""
    best, best_name = None, ""
    for name in names:
        try:
            mtime = (path / name).stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best:
            best, best_name = mtime, name
    return best, best_name


def install_state(path: Path, manager: Optional[str]) -> Optional[str]:
    """Why the installed tree cannot be trusted, or ``None`` if it looks current.

    A stale ``node_modules`` is worse than a missing one. A missing one is
    obvious; a stale one lets the checks run, and the first tool added since the
    last install fails with its own error - `next lint` reports "ESLint must be
    installed" - which reads as a lint failure in your branch rather than as an
    install that predates the devDependency. The top-level copy stays whatever
    got hoisted there at install time, even once the manifest says otherwise,
    because nothing rewrites it until an install runs.

    Staleness is read from mtimes, comparing package.json and the lockfile
    against the marker the manager leaves in `node_modules`. That is a
    heuristic, so it is pointed the safe way - every install command here is
    lockfile-frozen, which makes a false positive cost one reinstall and a
    false negative cost an afternoon.
    """
    node_modules = path / "node_modules"
    if not node_modules.exists():
        return "node_modules missing"

    installed, _ = _newest(path, INSTALL_MARKERS.get(manager or "", []))
    if installed is None:
        # No marker we recognise; the directory's own mtime is the best proxy.
        try:
            installed = node_modules.stat().st_mtime
        except OSError:
            return None

    newest, newest_name = _newest(path, INSTALL_INPUTS)
    if newest is not None and newest > installed:
        return f"node_modules is older than {newest_name}"
    return None


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
        frozen = _has_tracked_lockfile(path, manager)
        plan.install = Check("install", _node_install(manager, frozen), required=True)

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

        stale = install_state(path, manager)
        if stale:
            plan.notes.append(f"{stale} - install required")
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


# What `git rev-parse --local-env-vars` lists: the variables that pick a
# repository regardless of the directory git runs in.
GIT_LOCATION_VARS = frozenset({
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT", "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
})


def git_env() -> Dict[str, str]:
    """``os.environ`` minus the variables that override where git looks.

    git exports ``GIT_DIR`` into every hook - on a push from a worktree it is
    that worktree's private gitdir - and it outranks both ``cwd`` and
    ``git -C``. A question about "the repo at this path" asked from inside the
    pre-push hook has to drop it, or git answers about the pushing tree instead.
    """
    return {k: v for k, v in os.environ.items() if k not in GIT_LOCATION_VARS}


@dataclass
class Checkout:
    """One working tree, and the name the policy knows its repo by."""

    repo: str
    path: Path


def resolve_checkout(path: Path) -> Checkout:
    """The working-tree root containing ``path``, and its canonical repo name.

    A linked worktree sits somewhere like ``CI-Capture/.claude/worktrees/x``,
    so its own directory name means nothing to ``ci-local.toml``. The name that
    keys ``[checks.<repo>]`` and ``aliases`` is the main clone's - what
    :func:`discover_repos` calls it - and every worktree shares the main clone's
    git dir, so ``--git-common-dir`` leads back to it.

    Raises ``ValueError`` when ``path`` is not inside a working tree.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse",
             "--show-toplevel", "--git-common-dir"],
            capture_output=True, text=True, env=git_env(),
        )
    except OSError as exc:
        raise ValueError(f"cannot run git: {exc}") from exc
    lines = proc.stdout.splitlines()
    if proc.returncode or len(lines) != 2:
        raise ValueError(f"{path} is not inside a git working tree")

    root = Path(lines[0])
    # Relative to the directory git ran in, e.g. `../.git` from a subdirectory.
    common = (Path(path) / lines[1]).resolve()
    if common.name == ".git":
        repo = common.parent.name
    elif common.suffix == ".git":  # worktree of a bare repo: `CI-Capture.git`
        repo = common.stem
    else:  # `--separate-git-dir`: nothing to borrow a name from
        repo = root.name
    return Checkout(repo, root)


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
