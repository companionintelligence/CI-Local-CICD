"""Install a ``pre-push`` hook that runs the local checks before code leaves.

Gating the hosted workflows only saves money if the checks still happen. This
hook is where they move to. It is deliberately escapable - ``SKIP_LOCAL_CI=1``
or ``git push --no-verify`` - because a hook that cannot be bypassed gets
deleted by whoever it blocks first. It is not allowed to pass by accident:
when the ``ci-local`` it names is gone, the push is refused, not waved through.

Where the hook goes depends on ``core.hooksPath``. Without it, git runs
``.git/hooks/pre-push``: ci-local writes that file when there is none, and with
``--force`` puts its block at the top of one that is already there, such as
git-lfs's. With it - husky's ``.husky/_``, a ``.githooks`` directory - git
never looks in ``.git/hooks``, so a hook there is dead. ci-local then puts its
block, between markers, into the ``pre-push`` that git does run. Either way
everything else in a shared file is kept, and runs after the block.
"""

from __future__ import annotations

import functools
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .detect import git_env

HOOK_NAME = "pre-push"
SENTINEL = "# managed-by: ci-local"

# The block ci-local owns inside a hook file it shares. The markers never
# contain SENTINEL, so a shared file is never mistaken for one ci-local owns.
BLOCK_BEGIN = "# >>> ci-local pre-push"
BLOCK_END = "# <<< ci-local pre-push"
# On the BEGIN line when `hooks install` created the file, so that `hooks
# uninstall` deletes that file, and never one that was there before.
CREATED = "which created this file"

# What the hook runs unless `hooks install --only` says otherwise. `spec` is the
# spec slot: every `spec:<id>` check from the repo's `.agent-configs/specs.toml`.
DEFAULT_CHECKS = ["lint", "typecheck", "test", "spec"]

# The block is sh, so it goes only into a file that a POSIX shell runs.
SHELLS = ("sh", "bash", "dash", "ksh", "zsh")

# `--repo` precedes the subcommand: it is a global flag, and the whole point of
# the hook is that it runs *this* repo's checks. Getting that wrong scoped every
# push to all 33 workspace repos - 37 minutes of unrelated, mostly-red output
# instead of 26 seconds - which teaches people to reach for SKIP_LOCAL_CI=1.
# `ci-local` now also accepts `--repo` after the subcommand, so both orders
# work, but the canonical global-first form is what gets written here.
#
# `--path` is the other half of "this repo". The hook file lives in the git dir
# that every worktree shares, and `--repo` alone resolves against the workspace
# - so a push from `CI-Capture/.claude/worktrees/x` was gated on the main
# clone's checkout: a red `main` rejected a green branch, and a green `main`
# would pass a broken one. git starts the hook at the pushing worktree's root,
# so that root is what gets checked; `--repo` still names the policy entry.
#
# The block is POSIX sh, because it may sit in a husky or `.githooks` file run
# by sh. It exits only to refuse the push, so the rest of a shared file still
# runs after it, and it leaves stdin unread: git sends the pushed refs there,
# and the rest of the file (git-lfs, for one) may need them.

BLOCK_TEMPLATE = """{begin} (written by `ci-local hooks install`{created}; edits are lost)
# Runs this repo's checks locally instead of on billed GitHub-hosted runners.
# Bypass with SKIP_LOCAL_CI=1 or `git push --no-verify`.
if [ "${{SKIP_LOCAL_CI:-0}}" = "1" ]; then
  echo "local-ci: skipped (SKIP_LOCAL_CI=1)"
else
  CI_LOCAL="{cli}"
  # A gate that cannot run must not pass: refuse the push instead.
  if [ ! -f "$CI_LOCAL" ]; then
    echo "local-ci: $CI_LOCAL not found - push aborted." >&2
    echo "          restore it, or reinstall with: ci-local hooks install --apply" >&2
    echo "          bypass with: SKIP_LOCAL_CI=1 git push" >&2
    exit 1
  fi
  if ! command -v "{python}" >/dev/null 2>&1; then
    echo "local-ci: {python} not found - push aborted." >&2
    echo "          reinstall with: ci-local hooks install --apply" >&2
    echo "          bypass with: SKIP_LOCAL_CI=1 git push" >&2
    exit 1
  fi

  # The tree being pushed: every worktree runs this same file, from its own root.
  REPO_PATH="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

  echo "local-ci: running {checks} for {repo} in $REPO_PATH"
  if </dev/null "{python}" "$CI_LOCAL" --repo "{repo}" run --path "$REPO_PATH" --only "{checks}" --quiet
  then CI_LOCAL_STATUS=0
  else CI_LOCAL_STATUS=$?
  fi
  if [ "$CI_LOCAL_STATUS" -ne 0 ]; then
    echo "" >&2
    echo "local-ci: checks failed - push aborted." >&2
    echo "          re-run with: {python} $CI_LOCAL --repo {repo} run --path '$REPO_PATH'" >&2
    echo "          bypass with: SKIP_LOCAL_CI=1 git push" >&2
    exit "$CI_LOCAL_STATUS"
  fi
fi
{end}
"""

# The file ci-local owns in `.git/hooks`: nothing but the block.
HOOK_TEMPLATE = "#!/bin/sh\n{sentinel}\n{block}"


@dataclass
class HookResult:
    repo: str
    action: str  # "installed" | "updated" | "skipped" | "removed" | "live" | "dead" | "none"
    detail: str = ""


def git_dir(repo_path: Path) -> Optional[Path]:
    """Resolve ``.git`` for both normal clones and worktrees."""
    marker = repo_path / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        # Worktrees store `gitdir: /abs/path` in a plain file.
        try:
            content = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if content.startswith("gitdir:"):
            resolved = Path(content.split(":", 1)[1].strip())
            if not resolved.is_absolute():
                resolved = (repo_path / resolved).resolve()
            return resolved
    return None


@dataclass
class HookDirs:
    """Where ci-local's own hook lives, and where git looks for hooks."""

    root: Path    # the working tree
    own: Path     # `<git common dir>/hooks`: git's default, shared by every worktree
    active: Path  # the directory git runs hooks from
    configured: str = ""  # core.hooksPath as written, when it moves `active`
    # The main clone (or bare repository) when `root` is a linked worktree of it.
    main_clone: Optional[Path] = None

    @property
    def redirected(self) -> bool:
        return self.active != self.own

    @property
    def per_worktree(self) -> bool:
        """A relative core.hooksPath: each worktree resolves it against its own root."""
        return self.redirected and not os.path.isabs(os.path.expanduser(self.configured))

    @property
    def linked(self) -> bool:
        return self.main_clone is not None


def _git(cwd: Path, *args: str, ceiling: bool = False) -> subprocess.CompletedProcess:
    env = git_env()
    if ceiling:
        # Look at this directory only, never at a checkout that contains it.
        env["GIT_CEILING_DIRECTORIES"] = str(cwd.parent)
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        env=env, stdin=subprocess.DEVNULL,
    )


def hook_dirs(repo_path: Path) -> Optional[HookDirs]:
    """Ask git where hooks live for the checkout at ``repo_path``.

    git's own answer covers what a hand-written lookup gets wrong: a relative
    core.hooksPath is taken from the root of the worktree that pushes, `~` is
    expanded, and linked worktrees share the main clone's hooks. A directory
    git does not recognise - the bare ``.git`` folders some tests build - falls
    back to ``<gitdir>/hooks``.
    """
    root = repo_path.resolve()
    try:
        proc = _git(root, "rev-parse", "--show-toplevel", "--git-dir",
                    "--git-common-dir", "--git-path", "hooks", ceiling=True)
    except OSError:
        proc = None
    lines = proc.stdout.splitlines() if proc is not None and proc.returncode == 0 else []
    if len(lines) != 4 or Path(lines[0]).resolve() != root:
        gitdir = git_dir(repo_path)
        if gitdir is None:
            return None
        return HookDirs(repo_path, gitdir / "hooks", gitdir / "hooks")
    gitdir = (root / lines[1]).resolve()
    common = (root / lines[2]).resolve()
    own = (common / "hooks").resolve()
    active = (root / lines[3]).resolve()
    configured = ""
    if active != own:
        configured = _git(root, "config", "--get", "core.hooksPath").stdout.strip()
    main_clone = None
    if gitdir != common:
        main_clone = common.parent if common.name == ".git" else common
    return HookDirs(root, own, active, configured, main_clone)


def _linked(dirs: HookDirs) -> str:
    # A linked worktree shares the main clone's hooks, and the main clone's
    # name is the one that keys the policy. Installing from here would rewrite
    # that shared hook under this folder's name.
    return f"linked worktree of {dirs.main_clone}; manage the hook from the main clone"


def unstable_location(root: Path, temp_dirs: Optional[List[Path]] = None) -> Optional[str]:
    """Why a hook should not name the ``ci-local`` in checkout ``root``, if so.

    The hook fails closed, so the path it names has to outlive the hook. A
    linked worktree goes when its branch is done, and a temp directory goes in
    the next cleanup. After that, every push through the hook is refused.
    """
    dirs = hook_dirs(root)
    if dirs is not None and dirs.linked:
        return f"{root} is a linked worktree of {dirs.main_clone}"
    resolved = root.resolve()
    for temp in temp_dirs if temp_dirs is not None else _temp_dirs():
        if _within(resolved, temp):
            return f"{root} is under the temporary directory {temp}"
    return None


def _temp_dirs() -> List[Path]:
    names = {tempfile.gettempdir(), os.environ.get("TMPDIR") or "", "/tmp", "/var/tmp"}
    return sorted({Path(name).resolve() for name in names if name})


def render_block(
    repo: str, cli_path: Path, python: str, checks: List[str], created: bool = False,
) -> str:
    return BLOCK_TEMPLATE.format(
        begin=BLOCK_BEGIN,
        end=BLOCK_END,
        created=f", {CREATED}" if created else "",
        cli=cli_path,
        python=python,
        repo=repo,
        checks=",".join(checks),
    )


def render_hook(repo: str, cli_path: Path, python: str, checks: List[str]) -> str:
    return HOOK_TEMPLATE.format(
        sentinel=SENTINEL, block=render_block(repo, cli_path, python, checks),
    )


def install(
    repo_path: Path,
    cli_path: Path,
    python: str = "python3",
    checks: Optional[List[str]] = None,
    force: bool = False,
    edit_tracked: bool = False,
    apply: bool = False,
) -> HookResult:
    """Write the hook, or put the block into the hook git already runs.

    ``force`` lets the block into an existing ``.git/hooks/pre-push`` that
    ci-local did not write. ``edit_tracked`` lets it into a core.hooksPath hook
    that git tracks or would commit. They are separate because the second puts
    this machine's paths one commit away from a remote.

    The action is decided before anything is touched, so without ``apply`` the
    result is exactly what ``apply`` would report and nothing is written - not
    the hook, not even the hooks dir. ``hooks install`` used to write
    unconditionally, which rewrote the hook in every workspace repo for anyone
    who ran it expecting the usual dry run.
    """
    repo = repo_path.name
    dirs = hook_dirs(repo_path)
    if dirs is None:
        return HookResult(repo, "skipped", "not a git repository")
    if dirs.linked:
        return HookResult(repo, "skipped", _linked(dirs))
    checks = checks or DEFAULT_CHECKS
    render = functools.partial(render_block, repo, cli_path, python, checks)
    if dirs.redirected:
        return _chain(repo, dirs, dirs.active / HOOK_NAME, render, edit_tracked, apply)

    hook_path = dirs.own / HOOK_NAME
    if os.path.lexists(hook_path) and not _owned(hook_path):
        # Someone else's hook: git-lfs writes one. The block goes at its top and
        # the rest keeps running. It used to be moved aside, and never ran again.
        if not force and not _has_block(hook_path):
            return HookResult(repo, "skipped",
                              "existing unmanaged hook (use --force to add the "
                              "ci-local block to the top of it)")
        return _chain(repo, dirs, hook_path, render, edit_tracked, apply)

    body = render_hook(repo, cli_path, python, checks)
    action = "installed"
    if hook_path.exists():
        if hook_path.read_text(encoding="utf-8", errors="replace") == body:
            return HookResult(repo, "skipped", "already current")
        action = "updated"

    result = HookResult(repo, action, str(hook_path))
    if not apply:
        return result

    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(body, encoding="utf-8")
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return result


def _chain(
    repo: str,
    dirs: HookDirs,
    target: Path,
    render: Callable[..., str],
    edit_tracked: bool,
    apply: bool,
) -> HookResult:
    """Put the block at the top of ``target``, keeping the rest of the file."""
    problem, tracking = _cannot_edit(dirs, target, repo, edit_tracked)
    if problem:
        return HookResult(repo, "skipped", problem)

    created = not target.exists()
    span = None
    if created:
        text, updated = "", "#!/bin/sh\n" + render(created=True)
    else:
        try:
            text = target.read_text(encoding="utf-8")
            lines = text.splitlines(keepends=True)
            span = _find_block(lines)
        except UnicodeDecodeError:
            return HookResult(repo, "skipped", f"{target} is not UTF-8 text; not editing it")
        except ValueError as exc:
            return HookResult(repo, "skipped", f"{target}: {exc}")
        # A file ci-local created keeps saying so, for `hooks uninstall`.
        ours = span is not None and CREATED in lines[span[0]]
        updated = _with_block(lines, span, render(created=ours))

    # ci-local's whole-file hook from before core.hooksPath was honoured: git
    # does not run it, so it goes once the block is in place.
    retire = dirs.redirected and _owned(dirs.own / HOOK_NAME)
    if updated == text and not retire:
        return HookResult(repo, "skipped", "already current")

    action = "updated" if span is not None else "installed"
    where = f"core.hooksPath {dirs.configured}" if dirs.redirected else "added to the existing hook"
    detail = [f"{target} ({where})"]
    if tracking:
        detail.append(tracking)
    if retire:
        detail.append(f"removes the copy in {dirs.own / HOOK_NAME}, which git does not run")
    if dirs.per_worktree:
        detail.append("each linked worktree resolves core.hooksPath to its own copy")
    result = HookResult(repo, action, "; ".join(detail))
    if not apply:
        return result

    target.write_text(updated, encoding="utf-8")
    if created:
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    if retire:
        _remove_owned(dirs.own)
    return result


def _cannot_edit(
    dirs: HookDirs, target: Path, repo: str, edit_tracked: bool,
) -> Tuple[Optional[str], str]:
    """Why ci-local will not put its block into ``target``, or a note on doing so.

    The note is set when ``edit_tracked`` lets the block into a file git tracks
    or would commit, so that the dry run says so.
    """
    common = dirs.own.parent
    active = target.parent
    if not (_within(active, dirs.root) or _within(active, common)):
        # A directory outside the repo may serve every repo on the machine; a
        # block naming this repo would then run its checks on every push.
        return (f"core.hooksPath {dirs.configured} is outside this repo and may "
                f"be shared; not editing it"), ""
    if not active.is_dir():
        return (f"core.hooksPath {dirs.configured} does not exist, so git runs no "
                f"pre-push hook here; set the directory up first"), ""
    if target.is_symlink():
        return f"{target} is a symlink; not editing through it", ""
    if target.exists():
        if not os.access(target, os.X_OK):
            return f"{target} is not executable, so git does not run it", ""
        problem = _interpreter_problem(target)
        if problem:
            return problem, ""
    if _within(target, common):
        return None, ""  # inside the git dir, which git never tracks
    relative = str(target.relative_to(dirs.root))
    if _git(dirs.root, "ls-files", "--error-unmatch", "--", relative).returncode == 0:
        refusal = "tracked by git: the block would show as a local change"
        note = "tracked by git: the block will show as a local change"
    elif _git(dirs.root, "check-ignore", "-q", "--", relative).returncode != 0:
        refusal = note = "not ignored by git: the block could be committed"
    else:
        return None, ""
    if not edit_tracked:
        # The block names this machine's paths, and the repo may be public.
        return f"{target} is {refusal} (to edit it anyway: --repo {repo} --edit-tracked)", ""
    return None, note


def _interpreter_problem(target: Path) -> Optional[str]:
    """Why the program on ``target``'s ``#!`` line cannot run the block, if so."""
    try:
        with target.open("rb") as handle:
            first = handle.readline(512)
    except OSError as exc:
        return f"{target} cannot be read: {exc.strerror or type(exc).__name__}"
    if not first.startswith(b"#!"):
        return None  # git runs a hook without one with sh
    program = _shebang_program(first[2:].decode("utf-8", errors="replace"))
    if program in SHELLS:
        return None
    return (f"{target} is run by {program or 'an unnamed program'}, not a POSIX shell, "
            f"and the block is sh; not editing it")


def _shebang_program(line: str) -> str:
    """The program a ``#!`` line runs, looking through ``env``."""
    words = line.split()
    if not words:
        return ""
    program = os.path.basename(words[0])
    if program != "env":
        return program
    for word in words[1:]:
        # `env -S bash -e`, `env LC_ALL=C sh`: options and assignments come first.
        if word.startswith("-") or "=" in word:
            continue
        return os.path.basename(word)
    return ""


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _find_block(lines: List[str]) -> Optional[Tuple[int, int]]:
    """First and last line of ci-local's block, or None when there is none."""
    begins = [i for i, line in enumerate(lines) if line.startswith(BLOCK_BEGIN)]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == BLOCK_END]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise ValueError("its ci-local block markers are missing or repeated; "
                         "fix the file by hand")
    return begins[0], ends[0]


def _with_block(lines: List[str], span: Optional[Tuple[int, int]], block: str) -> str:
    """``lines`` with ci-local's block at ``span`` replaced, or added after the shebang."""
    if span is not None:
        begin, end = span
        return "".join(lines[:begin]) + block + "".join(lines[end + 1:])
    # First, so that an `exec` or `exit` later in the file cannot skip it.
    at = 1 if lines and lines[0].startswith("#!") else 0
    head = "".join(lines[:at])
    if head and not head.endswith("\n"):
        head += "\n"
    return head + block + "".join(lines[at:])


def _owned(path: Path) -> bool:
    """Whether ``path`` is a whole-file hook ci-local wrote."""
    try:
        return SENTINEL in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _has_block(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(line.startswith(BLOCK_BEGIN) for line in text.splitlines())


def _remove_owned(hooks_dir: Path) -> None:
    """Delete ci-local's hook, and put back a hook an older ci-local moved aside."""
    hook_path = hooks_dir / HOOK_NAME
    backup = hooks_dir / f"{HOOK_NAME}.pre-ci-local"
    hook_path.unlink()
    if backup.exists():
        backup.rename(hook_path)


def uninstall(repo_path: Path, apply: bool = False) -> HookResult:
    """Remove our hook or our block, putting back what was there before.

    As with :func:`install`, nothing changes on disk without ``apply``.
    """
    repo = repo_path.name
    dirs = hook_dirs(repo_path)
    if dirs is None:
        return HookResult(repo, "skipped", "not a git repository")
    if dirs.linked:
        return HookResult(repo, "skipped", _linked(dirs))
    if dirs.redirected:
        return _unchain(repo, dirs, dirs.active / HOOK_NAME, apply)

    hook_path = dirs.own / HOOK_NAME
    if not os.path.lexists(hook_path):
        return HookResult(repo, "skipped", "no hook installed")
    if not _owned(hook_path):
        if _has_block(hook_path):
            return _unchain(repo, dirs, hook_path, apply)
        return HookResult(repo, "skipped", "hook not managed by ci-local")

    # A hook an older ci-local moved aside with --force, which it now restores.
    backup = dirs.own / f"{HOOK_NAME}.pre-ci-local"
    restore = backup.exists()
    result = HookResult(
        repo, "removed", "restored previous hook" if restore else str(hook_path)
    )
    if not apply:
        return result

    _remove_owned(dirs.own)
    return result


def _unchain(repo: str, dirs: HookDirs, target: Path, apply: bool) -> HookResult:
    """Take the block out of ``target``, and any dead copy in ``.git/hooks`` too."""
    remaining = None
    created = False
    if _has_block(target):
        if target.is_symlink():
            return HookResult(repo, "skipped", f"{target} is a symlink; not editing through it")
        try:
            lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
            begin, end = _find_block(lines)
        except (UnicodeDecodeError, ValueError) as exc:
            return HookResult(repo, "skipped", f"{target}: {exc}")
        remaining = "".join(lines[:begin] + lines[end + 1:])
        # Deleted only when ci-local created it and nothing has been added since.
        created = CREATED in lines[begin] and all(
            line.startswith("#!") or not line.strip() for line in remaining.splitlines()
        )
    retire = dirs.redirected and _owned(dirs.own / HOOK_NAME)
    if remaining is None and not retire:
        return HookResult(repo, "skipped", "no hook installed")

    detail = []
    if remaining is not None:
        detail.append(f"{target}" + (" (deleted: ci-local created it)" if created else ""))
    if retire:
        detail.append(f"{dirs.own / HOOK_NAME}, which git does not run")
    result = HookResult(repo, "removed", "; ".join(detail))
    if not apply:
        return result

    if remaining is not None:
        if created:
            target.unlink()
        else:
            target.write_text(remaining, encoding="utf-8")
    if retire:
        _remove_owned(dirs.own)
    return result


def status(
    repo_path: Path,
    cli_path: Path,
    python: str = "python3",
    checks: Optional[List[str]] = None,
) -> HookResult:
    """Whether git will run ci-local's hook for this repo.

    ``live`` - git runs it; the detail says whether ``hooks install`` would
    rewrite it. ``dead`` - a ci-local hook is installed where git does not
    look, or git skips it. ``none`` - there is no ci-local hook.
    """
    repo = repo_path.name
    dirs = hook_dirs(repo_path)
    if dirs is None:
        return HookResult(repo, "skipped", "not a git repository")
    if dirs.linked:
        return HookResult(repo, "skipped", _linked(dirs))
    target = dirs.active / HOOK_NAME
    runs = _owned(target) or _has_block(target)

    dead = []
    if dirs.redirected and _owned(dirs.own / HOOK_NAME):
        dead.append(f"{dirs.own / HOOK_NAME} is not run: core.hooksPath is "
                    f"{dirs.configured}, so git runs {target}")
    if runs and not os.access(target, os.X_OK):
        dead.append(f"{target} is not executable, so git skips it")
        runs = False
    if not runs:
        if dead:
            return HookResult(repo, "dead", "; ".join(dead))
        # Under husky this is also what a reinstall of the repo's dependencies
        # leaves: husky rewrites `.husky/_`, block and all.
        where = f" (core.hooksPath {dirs.configured})" if dirs.redirected else ""
        return HookResult(repo, "none", f"no ci-local hook in {target}{where}")

    # The dry run of `hooks install` says whether a reinstall would change it.
    # Both flags only so a file that needs one is compared rather than refused.
    fresh = install(repo_path, cli_path, python, checks, force=True, edit_tracked=True)
    current = fresh.action == "skipped" and fresh.detail == "already current"
    notes = [f"{target} ({'current' if current else 'hooks install would rewrite it'})"]
    notes += dead + _unreached(dirs)
    return HookResult(repo, "live", "; ".join(notes))


def _unreached(dirs: HookDirs) -> List[str]:
    """Linked worktrees whose own core.hooksPath copy has no ci-local block.

    A relative core.hooksPath (husky's `.husky/_`, `.githooks`) is resolved
    from the root of the worktree that pushes, so each linked worktree runs its
    own copy, and the block in the main clone's copy does not gate their pushes.
    """
    if not dirs.per_worktree:
        return []
    listing = _git(dirs.root, "worktree", "list", "--porcelain")
    trees = [Path(line[len("worktree "):]) for line in listing.stdout.splitlines()
             if line.startswith("worktree ")]
    linked = [t for t in trees if t.resolve() != dirs.root and t.is_dir()]
    missing = 0
    for tree in linked:
        theirs = hook_dirs(tree)
        if theirs is not None and not _has_block(theirs.active / HOOK_NAME):
            missing += 1
    if not missing:
        return []
    return [f"{missing} of {len(linked)} linked worktree(s) resolve core.hooksPath "
            f"to their own {dirs.configured}, which has no ci-local block, so "
            f"their pushes are not checked"]
