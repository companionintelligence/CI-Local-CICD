"""``ci-local`` - gate hosted workflows, run the checks here instead.

    ci-local audit  [--usage report.csv]   what would change, and what it saves
    ci-local gate   [--repo X] [--apply]   rewrite workflows to manual-only
    ci-local restore [--repo X] [--apply]  put the original triggers back
    ci-local plan   [--repo X] [--path P]  show the detected local checks
    ci-local run    [--repo X] [--path P]  run those checks locally
    ci-local hooks install [--apply]       add the pre-push hook
    ci-local hooks uninstall [--apply]     remove it

Nothing writes to disk without ``--apply``; every command defaults to a dry run.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import __version__
from .detect import discover_repos, plan_for, resolve_checkout
from .hooks import install as install_hook
from .hooks import uninstall as uninstall_hook
from .policy import DEFAULT_POLICY_FILE, Policy, classify
from .retention import DEFAULT_RETENTION_DAYS, apply_retention
from .runner import DEFAULT_TIMEOUT, run_plans, summarize
from .ship import BRANCH, ship_repo, slug
from .docs import render as render_docs
from .schedule import install as install_watch
from .schedule import notify, uninstall as uninstall_watch
from .activity import DEFAULT_WINDOW_DAYS as ACTIVITY_WINDOW_DAYS
from .activity import DORMANT as ACTIVITY_DORMANT
from .activity import UNKNOWN as ACTIVITY_UNKNOWN
from .spend import linux_equivalent
from .verify import (
    BASELINE_FILE, audit_all, find_drift, measure_activity, snapshot,
)
from .workflows import (
    WorkflowParseError, gate, read_workflow, restore, write_workflow,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = REPO_ROOT.parent

STATUS_MARK = {
    "pass": "ok  ",
    "fail": "FAIL",
    "skip": "skip",
    "timeout": "TIME",
    "empty": "----",
}


def workflow_files(repo_path: Path) -> List[Path]:
    wf_dir = repo_path / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    return sorted(
        p
        for p in wf_dir.iterdir()
        if p.is_file() and p.suffix in (".yml", ".yaml")
    )


def load_usage(path: Path) -> Dict[Tuple[str, str], float]:
    """Net spend per (repo, workflow filename) from a GitHub usage report."""
    totals: Dict[Tuple[str, str], float] = defaultdict(float)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("product") != "actions":
                continue
            workflow = (row.get("workflow_path") or "").rsplit("/", 1)[-1]
            if not workflow:
                continue
            try:
                totals[(row.get("repository", ""), workflow)] += float(
                    row.get("net_amount") or 0
                )
            except ValueError:
                continue
    return dict(totals)


def spend_for(usage: Dict[Tuple[str, str], float], policy: Policy, decision) -> float:
    """Net spend for a decision, resolving the clone name to the GitHub name."""
    return usage.get((policy.github_name(decision.repo), decision.workflow), 0.0)


def unmatched_repos(
    usage: Dict[Tuple[str, str], float], decisions, policy: Policy
) -> List[Tuple[str, float]]:
    """Billed repos we never saw locally - money this workspace cannot reach.

    Without this, a repo that is billed but has no clone here (or is cloned
    somewhere the scan does not descend into) silently reads as $0 saved.
    """
    seen = {policy.github_name(d.repo) for _, d in decisions}
    totals: Dict[str, float] = defaultdict(float)
    for (repo, _), amount in usage.items():
        if repo and repo not in seen:
            totals[repo] += amount
    return sorted(totals.items(), key=lambda kv: -kv[1])


def collect_decisions(
    workspace: Path, policy: Policy, only: Optional[List[str]]
) -> List[Tuple[Path, "object"]]:
    """Classify every workflow in scope, pairing each decision with its file."""
    out = []
    for repo_path in discover_repos(workspace, only):
        if policy.is_excluded(repo_path.name):
            continue
        for wf in workflow_files(repo_path):
            try:
                text = read_workflow(wf)
            except OSError as exc:
                print(f"  ! {repo_path.name}/{wf.name}: {exc}", file=sys.stderr)
                continue
            try:
                decision = classify(repo_path.name, wf.name, text, policy)
            except WorkflowParseError as exc:
                print(f"  ~ {repo_path.name}/{wf.name}: skipped ({exc})")
                continue
            out.append((wf, decision))
    return out


def cmd_audit(args: argparse.Namespace) -> int:
    policy = Policy.load(args.policy)
    decisions = collect_decisions(args.workspace, policy, args.repo)
    usage = load_usage(args.usage) if args.usage else {}

    buckets: Dict[str, List] = defaultdict(list)
    for _, decision in decisions:
        buckets[decision.action].append(decision)

    print(f"policy: {policy.source or 'none (defaults)'}")
    print(f"workflows scanned: {len(decisions)}\n")

    for action in ("gate", "keep", "already-gated", "already-manual"):
        items = buckets.get(action, [])
        if not items:
            continue
        cost = sum(spend_for(usage, policy, d) for d in items)
        header = f"{action.upper()} ({len(items)})"
        if usage:
            header += f"  -  ${cost:,.2f}/mo attributed"
        print(header)
        for d in sorted(items, key=lambda x: -spend_for(usage, policy, x)):
            spend = spend_for(usage, policy, d)
            money = f"${spend:>7,.2f}  " if usage else ""
            triggers = ",".join(d.triggers) or "-"
            print(f"  {money}{d.ident:<58} {triggers}  [{d.reason}]")
        print()

    if usage:
        saved = sum(spend_for(usage, policy, d) for d in buckets.get("gate", []))
        print(f"estimated monthly reduction from gating: ${saved:,.2f}")
        unmatched = unmatched_repos(usage, decisions, policy)
        if unmatched:
            print("\nbilled repos with no local clone in this workspace:")
            for name, amount in unmatched:
                print(f"  ${amount:>7,.2f}  {name}")
    return 0


def _rewrite(args: argparse.Namespace, transform, verb: str) -> int:
    policy = Policy.load(args.policy)
    decisions = collect_decisions(args.workspace, policy, args.repo)

    changed = 0
    for wf, decision in decisions:
        if verb == "gate" and not decision.will_change:
            continue
        text = read_workflow(wf)
        try:
            new_text, did = transform(text)
        except WorkflowParseError as exc:
            print(f"  ~ {decision.ident}: skipped ({exc})")
            continue
        if not did:
            continue
        changed += 1
        print(f"  {'*' if args.apply else '-'} {decision.ident}")
        if args.apply:
            write_workflow(wf, new_text)

    if changed == 0:
        print(f"nothing to {verb}.")
    elif args.apply:
        print(f"\n{verb}d {changed} workflow(s).")
    else:
        print(f"\n{changed} workflow(s) would be {verb}d. Re-run with --apply.")
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    return _rewrite(args, gate, "gate")


def cmd_restore(args: argparse.Namespace) -> int:
    return _rewrite(args, restore, "restore")


def _plans(args: argparse.Namespace):
    policy = Policy.load(args.policy)
    if getattr(args, "path", None) is not None:
        return [_checkout_plan(args.path, args.repo, policy)]
    repos = discover_repos(args.workspace, args.repo)
    return [plan_for(p.name, p, policy.override_for(p.name)) for p in repos]


def _checkout_plan(path: Path, names: Optional[List[str]], policy: Policy):
    """The plan for one explicit checkout - what the pre-push hook runs.

    Hooks live in the git dir that every worktree of a repo shares, so a hook
    that named only ``--repo`` got the workspace's main clone back from
    ``discover_repos`` and checked whatever *it* had checked out, no matter
    which worktree was pushing: a red ``main`` rejected a green branch, and a
    green ``main`` would wave a broken one through. The tree comes from
    ``--path``; the name that keys the policy comes from ``--repo`` or, absent
    that, from the main clone the checkout belongs to.
    """
    if names and len(names) > 1:
        raise SystemExit("ci-local: --path is one checkout; pass at most one --repo")
    try:
        checkout = resolve_checkout(path)
    except ValueError as exc:
        raise SystemExit(f"ci-local: --path: {exc}")
    repo = names[0] if names else checkout.repo
    return plan_for(repo, checkout.path, policy.override_for(repo))


def cmd_plan(args: argparse.Namespace) -> int:
    for plan in _plans(args):
        if not plan.runnable and args.runnable_only:
            continue
        manager = f" ({plan.manager})" if plan.manager else ""
        print(f"{plan.repo}  [{plan.stack}{manager}]")
        for check in plan.checks:
            flag = "" if check.required else "  (optional)"
            print(f"    {check.name:<10} {check.display}{flag}")
        for note in plan.notes:
            print(f"    - {note}")
        print()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    plans = [p for p in _plans(args) if p.runnable]
    if not plans:
        print("no runnable repos matched.", file=sys.stderr)
        return 1

    only = args.only.split(",") if args.only else None
    results = run_plans(
        plans,
        only=only,
        install=args.install,
        timeout=args.timeout,
        jobs=args.jobs,
    )

    failures = 0
    for result in results:
        if result.status == "fail":
            failures += 1
        if args.quiet and result.status == "pass":
            continue
        print(f"{STATUS_MARK.get(result.status, '?')}  {result.repo}  "
              f"({result.duration:.0f}s)")
        for check in result.checks:
            if args.quiet and check.status in ("pass", "skip"):
                continue
            detail = check.reason or f"exit {check.returncode}"
            print(f"      {STATUS_MARK.get(check.status, '?')} {check.name:<10} "
                  f"{detail}")
            if check.output_tail and not args.quiet:
                for line in check.output_tail.splitlines():
                    print(f"          | {line}")

    summary = summarize(results)
    print(f"\n{summary['passed']}/{summary['repos']} repos passed in "
          f"{summary['duration_seconds']:.0f}s")
    if args.report:
        args.report.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"report written to {args.report}")
    return 1 if failures else 0


def cmd_retention(args: argparse.Namespace) -> int:
    """Cap artifact retention. Storage is billed wherever the job ran."""
    policy = Policy.load(args.policy)
    total = files = 0
    for repo_path in discover_repos(args.workspace, args.repo):
        if policy.is_excluded(repo_path.name):
            continue
        for wf in workflow_files(repo_path):
            text = read_workflow(wf)
            new_text, changes = apply_retention(text, days=args.days)
            if not changes:
                continue
            files += 1
            total += len(changes)
            print(f"  {'*' if args.apply else '-'} {repo_path.name}/{wf.name}")
            for change in changes:
                print(f"      line {change.line}: {change.action} ({change.detail})")
            if args.apply:
                write_workflow(wf, new_text)

    if not total:
        print("every upload-artifact step is already within the cap.")
    elif args.apply:
        print(f"\ncapped {total} upload(s) across {files} file(s).")
    else:
        print(f"\n{total} upload(s) across {files} file(s) would be capped at "
              f"{args.days} days. Re-run with --apply.")
    return 0


def cmd_ship(args: argparse.Namespace) -> int:
    policy = Policy.load(args.policy)
    usage = load_usage(args.usage) if args.usage else {}
    worktree_root = args.worktree_root
    worktree_root.mkdir(parents=True, exist_ok=True)

    shipped = failed = 0
    for repo_path in discover_repos(args.workspace, args.repo):
        if policy.is_excluded(repo_path.name):
            continue
        result = ship_repo(
            repo_path, policy, worktree_root, usage=usage,
            branch=args.branch, apply=args.apply,
            retention_days=args.retention_days,
            concurrency=args.concurrency,
            gating=not args.no_gate,
            write_docs=args.write_docs,
        )
        if result.status == "shipped":
            shipped += 1
        elif result.status == "failed":
            failed += 1
        elif result.status == "skipped" and not args.verbose:
            continue
        parts = []
        if result.workflows:
            parts.append(f"{len(result.workflows)} gated")
        if result.retention:
            parts.append(f"{len(result.retention)} retention")
        if result.concurrency:
            parts.append(f"{len(result.concurrency)} concurrency")
        if result.docs:
            parts.append("docs")
        count = f"{', '.join(parts):<22}" if parts else " " * 22
        print(f"  {result.status:<8} {result.repo:<30} {count}  {result.detail}")

    verb = "opened" if args.apply else "would open"
    print(f"\n{verb} {shipped} PR(s); {failed} failed.")
    if not args.apply:
        print("Re-run with --apply to push branches and open PRs.")
    return 1 if failed else 0


def cmd_verify(args: argparse.Namespace) -> int:
    """What is live on each default branch - the only thing that bills."""
    policy = Policy.load(args.policy)
    usage = load_usage(args.usage) if args.usage else {}

    pairs = []
    for repo_path in discover_repos(args.workspace, args.repo):
        if policy.is_excluded(repo_path.name):
            continue
        remote = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True, text=True,
        )
        if remote.returncode:
            continue
        pairs.append((repo_path.name, slug(remote.stdout)))

    audits = audit_all(pairs, policy)

    baseline_path = args.baseline or (REPO_ROOT / BASELINE_FILE)
    current = snapshot(audits)

    if args.save_baseline:
        baseline_path.write_text(json.dumps(current, indent=2, sort_keys=True),
                                 encoding="utf-8")
        print(f"baseline written: {baseline_path} ({len(current)} repos)")
        return 0

    if args.check_drift:
        if not baseline_path.exists():
            print(f"no baseline at {baseline_path}; run "
                  f"`ci-local verify --save-baseline` first.", file=sys.stderr)
            return 2
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        drifts = find_drift(baseline, current)
        if not drifts:
            print("no drift: every repo is at or better than the baseline.")
            return 0
        print(f"DRIFT: {len(drifts)} finding(s)")
        for drift in drifts:
            print(f"  - {drift.describe()}")
        if args.notify:
            notify("CI Actions drift",
                   "; ".join(d.describe() for d in drifts[:3]))
        return 1

    want_activity = getattr(args, "activity", False)
    if want_activity:
        # `dict(pairs)`, not `policy.github_name`: the API needs `owner/repo`,
        # and a bare name silently 404s into "unknown" for every workflow.
        measure_activity(audits, dict(pairs))

    still_billing = clean = 0
    outstanding = 0.0
    rows = []
    for audit in sorted(audits, key=lambda a: a.repo):
        if audit.error:
            rows.append((audit.repo, f"! {audit.error}", []))
            continue
        counts = audit.counts
        summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        if audit.live_billing:
            still_billing += 1
        else:
            clean += 1
        rows.append((f"{audit.repo} [{audit.base}]", summary, audit.live_billing))

    # The table lists every workflow that is billing today - kept ones marked
    # `keep`, not omitted. Leaving them out is what let a live production
    # deploy sit in the report as free.
    for name, summary, billing in rows:
        marker = "!" if billing else " "
        print(f" {marker} {name:<40} {summary}")
        for wf in billing:
            spend = usage.get(
                (policy.github_name(name.split(" [")[0]), wf.name), 0.0
            )
            outstanding += spend
            money = f"${spend:>7,.2f}  " if usage else ""
            tag = " (keep)" if wf.kept else ""
            print(f"      {money}{wf.name:<40} "
                  f"{','.join(wf.billable)}{tag}")

    print(f"\n{clean} repo(s) billing nothing; {still_billing} have live "
          f"billable triggers on their default branch.")
    live = [w for a in audits if not a.error for w in a.live_billing]
    print(f"live billing: {len(live)} workflow(s), "
          f"{linux_equivalent(w.spend for w in live)}x linux-equivalent"
          f"  ({sum(1 for w in live if w.kept)} of them on the keep list)")
    if usage:
        print(f"attributed spend on live billable workflows: "
              f"${outstanding:,.2f}/month")

    if want_activity:
        # Third axis, reported separately so it never renames a state. An idle
        # live-billing workflow still bills the next time anyone pushes.
        # `state == DORMANT`, never `not ran_recently`: an unreadable run
        # history is UNKNOWN, and printing it as idle is the same mistake one
        # axis down.
        idle = [w for w in live
                if w.activity and w.activity.state == ACTIVITY_DORMANT]
        unknown = [w for w in live
                   if w.activity and w.activity.state == ACTIVITY_UNKNOWN]
        if idle:
            print(f"\n{len(idle)} live billable workflow(s) have not run in "
                  f"{ACTIVITY_WINDOW_DAYS}d - candidates to retire, not "
                  f"evidence they are free:")
            for wf in idle:
                print(f"      {wf.name:<40} {wf.activity.describe()}")
        if unknown:
            print(f"\n{len(unknown)} live billable workflow(s) have no readable "
                  f"run history; they are counted as billing regardless.")

    # Gating is a switch, and restoring a repo turns its bill back on - so
    # report what is *switched off*, weighted by runner OS. This list is
    # gated-only by construction: a live workflow can never appear here.
    dormant = [(a, a.restore_cost)
               for a in sorted(audits, key=lambda a: a.repo) if not a.error]
    dormant = [(a, wfs) for a, wfs in dormant if wfs]
    if dormant:
        print(f"\n{len(dormant)} repo(s) are not billing only because they are "
              f"gated. Restoring them resumes spend:")
        for audit, wfs in dormant:
            weighted = linux_equivalent(w.spend for w in wfs)
            print(f"   {audit.repo:<38} {len(wfs)} workflow(s), "
                  f"{weighted}x linux-equivalent")
            for wf in wfs:
                print(f"      {wf.name:<40} {wf.verdict.reason}")
    return 0


def cmd_docs(args: argparse.Namespace) -> int:
    """Regenerate the developer runbook from the live default branches."""
    policy = Policy.load(args.policy)
    repos = [p for p in discover_repos(args.workspace, args.repo)
             if not policy.is_excluded(p.name)]
    slugs = {p.name: policy.github_name(p.name) for p in repos}
    pairs = [(p.name, f"companionintelligence/{slugs[p.name]}") for p in repos]

    audits = audit_all(pairs, policy)
    plans = {p.name: plan_for(p.name, p, policy.override_for(p.name))
             for p in repos}

    text = render_docs(audits, plans, slugs)
    args.output.write_text(text, encoding="utf-8")
    documented = sum(1 for a in audits if a.workflows)
    print(f"wrote {args.output} ({documented} repos, {len(text.splitlines())} lines)")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    binary = (REPO_ROOT / "ci-local").resolve()
    if args.action == "install":
        result = install_watch(binary, args.workspace.resolve(), hour=args.hour)
        print(("installed: " if result.installed else "NOT installed: ")
              + result.message)
        for path in result.paths:
            print(f"  {path}")
        return 0 if result.installed else 1
    if args.action == "uninstall":
        result = uninstall_watch()
        print(result.message)
        return 0
    plist = Path.home() / "Library" / "LaunchAgents" / \
        "com.companionintelligence.ci-local-drift.plist"
    timer = Path.home() / ".config" / "systemd" / "user" / "ci-local-drift.timer"
    found = [p for p in (plist, timer) if p.exists()]
    print("installed: " + ", ".join(str(p) for p in found) if found
          else "not installed")
    return 0


def cmd_hooks(args: argparse.Namespace) -> int:
    policy = Policy.load(args.policy)
    cli_path = REPO_ROOT / "ci-local"
    checks = args.only.split(",") if args.only else None
    pending: Dict[str, int] = defaultdict(int)
    for repo_path in discover_repos(args.workspace, args.repo):
        if args.action == "install":
            plan = plan_for(repo_path.name, repo_path, policy.override_for(repo_path.name))
            if not plan.runnable:
                print(f"  skip     {repo_path.name}  (no local checks detected)")
                continue
            result = install_hook(
                repo_path, cli_path, sys.executable, checks,
                force=args.force, apply=args.apply,
            )
        else:
            result = uninstall_hook(repo_path, apply=args.apply)
        if result.action != "skipped":
            pending[result.action] += 1
        print(f"  {result.action:<10}{result.repo}  {result.detail}")

    if args.apply:
        return 0
    if not pending:
        print("\ndry run: no hooks to change.")
    else:
        counts = ", ".join(f"{n} {action}" for action, n in pending.items())
        print(f"\ndry run: nothing written ({counts}). Re-run with --apply.")
    return 0


REPO_HELP = "limit to this repo (repeatable); valid before or after the subcommand"
PATH_HELP = (
    "check the checkout at this path (e.g. a worktree) instead of the "
    "workspace clone; config is looked up under --repo, else its main clone"
)


def add_command(sub, name: str, **kwargs) -> argparse.ArgumentParser:
    """A subparser that accepts ``--repo`` and refuses abbreviated flags.

    Both halves fix the same footgun. argparse only honours a *global* flag
    before the subcommand, so ``run --repo X`` never reached ``discover_repos``
    - and because ``--repo`` is a unique prefix of ``run``'s ``--report``, it
    was silently absorbed as a report path instead of erroring. The run then
    quietly covered all 33 workspace repos, which is what the pre-push hook was
    doing on every push. ``allow_abbrev=False`` stops any flag being swallowed
    by a longer one; the per-subcommand ``--repo`` makes the order people
    actually type work. ``SUPPRESS`` keeps the global value when this one is
    absent, so ``ci-local --repo X run`` still scopes correctly.
    """
    parser = sub.add_parser(name, allow_abbrev=False, **kwargs)
    parser.add_argument(
        "--repo", action="append", default=argparse.SUPPRESS, help=REPO_HELP,
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ci-local", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--workspace", type=Path, default=DEFAULT_WORKSPACE,
        help=f"workspace root (default: {DEFAULT_WORKSPACE})",
    )
    parser.add_argument(
        "--policy", type=Path, default=REPO_ROOT / DEFAULT_POLICY_FILE,
        help="policy file listing workflows to leave alone",
    )
    parser.add_argument("--repo", action="append", help=REPO_HELP)
    sub = parser.add_subparsers(dest="command", required=True)

    audit = add_command(sub, "audit", help="show what gating would do")
    audit.add_argument("--usage", type=Path, help="GitHub usage report CSV")
    audit.set_defaults(func=cmd_audit)

    gate_p = add_command(sub, "gate", help="rewrite workflows to manual-only")
    gate_p.add_argument("--apply", action="store_true", help="write changes")
    gate_p.set_defaults(func=cmd_gate)

    restore_p = add_command(sub, "restore", help="undo gating")
    restore_p.add_argument("--apply", action="store_true", help="write changes")
    restore_p.set_defaults(func=cmd_restore)

    plan_p = add_command(sub, "plan", help="show detected local checks")
    plan_p.add_argument(
        "--runnable-only", action="store_true",
        help="hide repos with no detected checks",
    )
    plan_p.add_argument("--path", type=Path, help=PATH_HELP)
    plan_p.set_defaults(func=cmd_plan)

    run_p = add_command(sub, "run", help="run local checks")
    run_p.add_argument("--path", type=Path, help=PATH_HELP)
    run_p.add_argument("--only", help="comma-separated check names")
    run_p.add_argument("--install", action="store_true", help="force dependency install")
    run_p.add_argument("--jobs", type=int, default=4, help="repos in parallel")
    run_p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    run_p.add_argument("--quiet", action="store_true", help="only report failures")
    run_p.add_argument("--report", type=Path, help="write a JSON report here")
    run_p.set_defaults(func=cmd_run)

    ship_p = add_command(
        sub, "ship", help="gate on a clean worktree and open a PR per repo"
    )
    ship_p.add_argument("--apply", action="store_true",
                        help="push branches and open PRs")
    ship_p.add_argument("--branch", default=BRANCH)
    ship_p.add_argument("--usage", type=Path, help="usage CSV, to cite savings")
    ship_p.add_argument(
        "--retention-days", type=int, default=None,
        help="also cap artifact retention-days in the same PR",
    )
    ship_p.add_argument("--write-docs", action="store_true",
                        help=f"also write a per-repo docs/CI.md")
    ship_p.add_argument("--no-gate", action="store_true",
                        help="skip trigger gating (e.g. concurrency-only PRs)")
    ship_p.add_argument("--concurrency", action="store_true",
                        help="also add a concurrency block where missing")
    ship_p.add_argument("--verbose", action="store_true",
                        help="also list skipped repos")
    ship_p.add_argument(
        "--worktree-root", type=Path,
        default=Path("/tmp/ci-local-worktrees"),
        help="where throwaway worktrees are created",
    )
    ship_p.set_defaults(func=cmd_ship)

    ret_p = add_command(
        sub, "retention", help="cap artifact retention-days (storage cost)"
    )
    ret_p.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS)
    ret_p.add_argument("--apply", action="store_true", help="write changes")
    ret_p.set_defaults(func=cmd_retention)

    verify_p = add_command(
        sub, "verify", help="audit what is live on each default branch"
    )
    verify_p.add_argument("--usage", type=Path, help="usage CSV, to cite cost")
    verify_p.add_argument("--baseline", type=Path,
                          help=f"baseline file (default: {BASELINE_FILE})")
    verify_p.add_argument("--save-baseline", action="store_true",
                          help="record current state as the expected state")
    verify_p.add_argument("--check-drift", action="store_true",
                          help="exit 1 if any repo is worse than the baseline")
    verify_p.add_argument(
        "--activity", action="store_true",
        help=f"also read run history ({ACTIVITY_WINDOW_DAYS}d) for billable "
             f"workflows; one extra API call each",
    )
    verify_p.add_argument("--notify", action="store_true",
                          help="send a desktop notification on drift")
    verify_p.set_defaults(func=cmd_verify)

    docs_p = add_command(
        sub, "docs", help="regenerate the developer runbook"
    )
    docs_p.add_argument("--output", type=Path,
                        default=REPO_ROOT / "RUNBOOK.md")
    docs_p.set_defaults(func=cmd_docs)

    watch_p = add_command(
        sub, "watch", help="install/remove the recurring drift check"
    )
    watch_p.add_argument("action", choices=["install", "uninstall", "status"])
    watch_p.add_argument("--hour", type=int, default=9,
                         help="local hour to run at (default 9)")
    watch_p.set_defaults(func=cmd_watch)

    hooks_p = add_command(sub, "hooks", help="manage the pre-push hook")
    hooks_p.add_argument("action", choices=["install", "uninstall"])
    hooks_p.add_argument("--apply", action="store_true", help="write changes")
    hooks_p.add_argument("--only", help="comma-separated checks for the hook")
    hooks_p.add_argument(
        "--force", action="store_true",
        help="back up and replace an existing unmanaged hook",
    )
    hooks_p.set_defaults(func=cmd_hooks)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
