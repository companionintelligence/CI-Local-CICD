"""Shipping gating as a PR, driven by a fake command runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_ci.policy import Policy
from local_ci.ship import (
    BRANCH,
    CommandResult,
    commit_message,
    default_branch,
    gate_worktree,
    pr_body,
    ship_repo,
    slug,
)

from .test_workflows import MANUAL_ONLY, MAPPING


class FakeRunner:
    """Records commands and replays canned results keyed by a command prefix."""

    def __init__(self, responses=None, on_call=None):
        self.responses = responses or {}
        self.calls = []
        self.on_call = on_call

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        self.calls.append((argv, cwd))
        if self.on_call:
            self.on_call(argv, cwd)
        for prefix, result in self.responses.items():
            if argv[: len(prefix)] == list(prefix):
                return result
        return CommandResult(0, "", "")

    def ran(self, *prefix) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c, _ in self.calls)

    def find(self, *prefix):
        for argv, _ in self.calls:
            if argv[: len(prefix)] == list(prefix):
                return argv
        return None


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/companionintelligence/CI-Hub.git",
         "companionintelligence/CI-Hub"),
        ("https://github.com/companionintelligence/CI-Import-Tools",
         "companionintelligence/CI-Import-Tools"),
        ("git@github.com:companionintelligence/CI-Server.git",
         "companionintelligence/CI-Server"),
        ("ssh://git@github.com/mrhegemon/cache-mounts.git",
         "mrhegemon/cache-mounts"),
        ("  https://github.com/org/repo.git\n", "org/repo"),
    ],
)
def test_slug_parses_remote_urls(url, expected):
    assert slug(url) == expected


class TestDefaultBranch:
    def test_prefers_github(self, tmp_path):
        run = FakeRunner({("gh", "repo", "view"): CommandResult(0, "dev\n")})
        assert default_branch("org/CI-Hub", tmp_path, run) == "dev"

    def test_falls_back_to_origin_head(self, tmp_path):
        run = FakeRunner({
            ("gh", "repo", "view"): CommandResult(1, "", "not found"),
            ("git", "symbolic-ref"): CommandResult(0, "origin/main\n"),
        })
        assert default_branch("org/x", tmp_path, run) == "main"

    def test_returns_none_when_unresolvable(self, tmp_path):
        run = FakeRunner({
            ("gh", "repo", "view"): CommandResult(1, "", "err"),
            ("git", "symbolic-ref"): CommandResult(1, "", "err"),
        })
        assert default_branch("org/x", tmp_path, run) is None

    def test_never_guesses_main(self, tmp_path):
        """A repo defaulting to `dev` must not be shipped against `main`."""
        run = FakeRunner({("gh", "repo", "view"): CommandResult(0, "dev\n")})
        assert default_branch("org/CI-Marketplace", tmp_path, run) != "main"


class TestGateWorktree:
    def _worktree(self, tmp_path, files):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        for name, text in files.items():
            (wf / name).write_text(text, encoding="utf-8")
        return tmp_path

    def test_gates_eligible_files_only(self, tmp_path):
        wt = self._worktree(tmp_path, {
            "ci.yml": MAPPING,
            "release.yml": MANUAL_ONLY,
            "notes.md": "ignored",
        })
        policy = Policy()
        gated, capped, concurrent = gate_worktree(wt, "CI-Server", policy)
        assert gated == ["ci.yml"]
        assert capped == []
        assert concurrent == []
        gated = (wt / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        assert "ci-local:gated" in gated

    def test_respects_the_keep_list(self, tmp_path):
        wt = self._worktree(tmp_path, {"deploy.yml": MAPPING})
        policy = Policy(keep=["CI-Portal/deploy.yml"])
        assert gate_worktree(wt, "CI-Portal", policy) == ([], [], [])

    def test_missing_workflows_dir(self, tmp_path):
        assert gate_worktree(tmp_path, "X", Policy()) == ([], [], [])


class TestMessages:
    def test_commit_message_has_subject_and_count(self):
        msg = commit_message(["ci.yml", "e2e.yml"])
        assert msg.splitlines()[0].startswith("chore(ci):")
        assert "Gated 2 workflow(s)." in msg

    def test_pr_body_lists_workflows_and_revert(self):
        body = pr_body("CI-Server", ["ci.yml"], 58.17)
        assert "- `ci.yml`" in body
        assert "$58.17/month" in body
        assert "ci-local restore --repo CI-Server --apply" in body

    def test_pr_body_without_cost_data(self):
        assert "/month" not in pr_body("X", ["ci.yml"], 0.0)


class TestShipRepo:
    def _repo(self, tmp_path):
        repo = tmp_path / "CI-Server"
        repo.mkdir()
        return repo

    def _seed_worktree(self, worktree_root, repo_name, files):
        """Simulate `git worktree add` materialising files on the base branch."""
        def handler(argv, cwd):
            if argv[:3] == ["git", "worktree", "add"]:
                wf = worktree_root / repo_name / ".github" / "workflows"
                wf.mkdir(parents=True, exist_ok=True)
                for name, text in files.items():
                    (wf / name).write_text(text, encoding="utf-8")
        return handler

    def test_dry_run_makes_no_commit(self, tmp_path):
        repo = self._repo(tmp_path)
        wt_root = tmp_path / "wt"
        run = FakeRunner(
            {("git", "remote", "get-url"): CommandResult(
                0, "https://github.com/org/CI-Server.git\n"),
             ("gh", "repo", "view"): CommandResult(0, "main\n")},
            on_call=self._seed_worktree(wt_root, "CI-Server", {"ci.yml": MAPPING}),
        )
        result = ship_repo(repo, Policy(), wt_root, run=run, apply=False)
        assert result.status == "dry-run"
        assert result.workflows == ["ci.yml"]
        assert result.base == "main"
        assert not run.ran("git", "commit")
        assert not run.ran("git", "push")
        assert not run.ran("gh", "pr", "create")

    def test_apply_commits_pushes_and_opens_a_pr(self, tmp_path):
        repo = self._repo(tmp_path)
        wt_root = tmp_path / "wt"
        run = FakeRunner(
            {("git", "remote", "get-url"): CommandResult(
                0, "https://github.com/org/CI-Server.git\n"),
             ("gh", "repo", "view"): CommandResult(0, "dev\n"),
             ("gh", "pr", "create"): CommandResult(
                 0, "https://github.com/org/CI-Server/pull/42\n")},
            on_call=self._seed_worktree(wt_root, "CI-Server", {"ci.yml": MAPPING}),
        )
        result = ship_repo(repo, Policy(), wt_root, run=run, apply=True)
        assert result.status == "shipped"
        assert result.pr_url.endswith("/pull/42")
        assert run.ran("git", "commit")

        # The PR must target the real default branch, not `main`.
        pr = run.find("gh", "pr", "create")
        assert pr[pr.index("--base") + 1] == "dev"
        # Only workflow files are staged.
        add = run.find("git", "add")
        assert add[-1] == ".github/workflows"

    def test_worktree_is_created_off_the_default_branch(self, tmp_path):
        repo = self._repo(tmp_path)
        wt_root = tmp_path / "wt"
        run = FakeRunner(
            {("git", "remote", "get-url"): CommandResult(
                0, "https://github.com/org/CI-Hub.git\n"),
             ("gh", "repo", "view"): CommandResult(0, "dev\n")},
            on_call=self._seed_worktree(wt_root, "CI-Server", {"ci.yml": MAPPING}),
        )
        ship_repo(repo, Policy(), wt_root, run=run, apply=False)
        add = run.find("git", "worktree", "add")
        assert add[-1] == "origin/dev"
        assert BRANCH in add

    def test_worktree_is_always_cleaned_up(self, tmp_path):
        repo = self._repo(tmp_path)
        wt_root = tmp_path / "wt"
        run = FakeRunner(
            {("git", "remote", "get-url"): CommandResult(
                0, "https://github.com/org/CI-Server.git\n"),
             ("gh", "repo", "view"): CommandResult(0, "main\n")},
            on_call=self._seed_worktree(wt_root, "CI-Server", {"ci.yml": MAPPING}),
        )
        ship_repo(repo, Policy(), wt_root, run=run, apply=False)
        assert run.ran("git", "worktree", "remove")
        assert run.ran("git", "worktree", "prune")

    def test_nothing_to_gate_is_skipped(self, tmp_path):
        repo = self._repo(tmp_path)
        wt_root = tmp_path / "wt"
        run = FakeRunner(
            {("git", "remote", "get-url"): CommandResult(
                0, "https://github.com/org/X.git\n"),
             ("gh", "repo", "view"): CommandResult(0, "main\n")},
            on_call=self._seed_worktree(wt_root, "CI-Server",
                                        {"release.yml": MANUAL_ONLY}),
        )
        result = ship_repo(repo, Policy(), wt_root, run=run, apply=True)
        assert result.status == "skipped"
        assert "nothing to change on main" in result.detail
        assert not run.ran("git", "commit")

    def test_missing_remote_is_skipped(self, tmp_path):
        run = FakeRunner({("git", "remote", "get-url"): CommandResult(1, "", "no")})
        result = ship_repo(self._repo(tmp_path), Policy(), tmp_path / "wt", run=run)
        assert result.status == "skipped"
        assert result.detail == "no origin remote"

    def test_unresolvable_default_branch_fails_loudly(self, tmp_path):
        run = FakeRunner({
            ("git", "remote", "get-url"): CommandResult(
                0, "https://github.com/org/X.git\n"),
            ("gh", "repo", "view"): CommandResult(1, "", "err"),
            ("git", "symbolic-ref"): CommandResult(1, "", "err"),
        })
        result = ship_repo(self._repo(tmp_path), Policy(), tmp_path / "wt", run=run)
        assert result.status == "failed"
        assert "default branch" in result.detail

    def test_fetch_failure_stops_before_worktree(self, tmp_path):
        run = FakeRunner({
            ("git", "remote", "get-url"): CommandResult(
                0, "https://github.com/org/X.git\n"),
            ("gh", "repo", "view"): CommandResult(0, "main\n"),
            ("git", "fetch"): CommandResult(1, "", "network down"),
        })
        result = ship_repo(self._repo(tmp_path), Policy(), tmp_path / "wt", run=run)
        assert result.status == "failed"
        assert "fetch failed" in result.detail
        assert not run.ran("git", "worktree", "add")

    def test_existing_pr_is_not_an_error(self, tmp_path):
        repo = self._repo(tmp_path)
        wt_root = tmp_path / "wt"
        run = FakeRunner(
            {("git", "remote", "get-url"): CommandResult(
                0, "https://github.com/org/CI-Server.git\n"),
             ("gh", "repo", "view"): CommandResult(0, "main\n"),
             ("gh", "pr", "create"): CommandResult(
                 1, "", "a pull request for branch already exists")},
            on_call=self._seed_worktree(wt_root, "CI-Server", {"ci.yml": MAPPING}),
        )
        result = ship_repo(repo, Policy(), wt_root, run=run, apply=True)
        assert result.status == "shipped"
        assert "already open" in result.detail

    def test_push_failure_skips_pr_creation(self, tmp_path):
        repo = self._repo(tmp_path)
        wt_root = tmp_path / "wt"
        run = FakeRunner(
            {("git", "remote", "get-url"): CommandResult(
                0, "https://github.com/org/CI-Server.git\n"),
             ("gh", "repo", "view"): CommandResult(0, "main\n"),
             ("git", "push"): CommandResult(1, "", "protected branch")},
            on_call=self._seed_worktree(wt_root, "CI-Server", {"ci.yml": MAPPING}),
        )
        result = ship_repo(repo, Policy(), wt_root, run=run, apply=True)
        assert result.status == "failed"
        assert "push failed" in result.detail
        assert not run.ran("gh", "pr", "create")

    def test_cost_is_cited_using_the_repo_alias(self, tmp_path):
        repo = tmp_path / "CI-Applet"
        repo.mkdir()
        wt_root = tmp_path / "wt"
        captured = {}
        run = FakeRunner(
            {("git", "remote", "get-url"): CommandResult(
                0, "https://github.com/org/CI-Capture.git\n"),
             ("gh", "repo", "view"): CommandResult(0, "main\n"),
             ("gh", "pr", "create"): CommandResult(0, "url\n")},
            on_call=self._seed_worktree(wt_root, "CI-Applet", {"ci.yml": MAPPING}),
        )
        policy = Policy(aliases={"CI-Applet": "CI-Capture"})
        ship_repo(repo, policy, wt_root, usage={("CI-Capture", "ci.yml"): 13.46},
                  run=run, apply=True)
        pr = run.find("gh", "pr", "create")
        assert "$13.46/month" in pr[pr.index("--body") + 1]


UPLOADER = """\
name: Build
on:
  push:
    branches: [main]
jobs:
  build:
    steps:
      - uses: actions/upload-artifact@v4
        with:
          path: dist/
"""


class TestShipWithRetention:
    def test_retention_applies_to_kept_workflows_too(self, tmp_path):
        """Storage is billed regardless of trigger, so a deploy workflow left
        on its automatic triggers must still get its retention capped."""
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "deploy.yml").write_text(UPLOADER, encoding="utf-8")

        policy = Policy(keep=["CI-Portal/deploy.yml"])
        gated, capped, _ = gate_worktree(tmp_path, "CI-Portal", policy,
                                         retention_days=7)
        assert gated == []
        assert capped == ["deploy.yml"]
        assert "retention-days: 7" in (wf / "deploy.yml").read_text()

    def test_gating_and_retention_combine_in_one_file(self, tmp_path):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(UPLOADER, encoding="utf-8")

        gated, capped, _ = gate_worktree(tmp_path, "CI-Hub", Policy(),
                                         retention_days=7)
        assert gated == ["ci.yml"] and capped == ["ci.yml"]
        text = (wf / "ci.yml").read_text()
        assert "ci-local:gated" in text
        assert "retention-days: 7" in text

    def test_retention_off_by_default(self, tmp_path):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(UPLOADER, encoding="utf-8")
        _, capped, _ = gate_worktree(tmp_path, "X", Policy())
        assert capped == []
        assert "retention-days" not in (wf / "ci.yml").read_text()

    def test_retention_only_repo_still_ships(self, tmp_path):
        """A repo whose workflows are all kept, but which uploads artifacts,
        must not be skipped - there is still a saving to make."""
        repo = tmp_path / "CI-Portal"
        repo.mkdir()
        wt_root = tmp_path / "wt"

        def seed(argv, cwd):
            if argv[:3] == ["git", "worktree", "add"]:
                d = wt_root / "CI-Portal" / ".github" / "workflows"
                d.mkdir(parents=True, exist_ok=True)
                (d / "deploy.yml").write_text(UPLOADER, encoding="utf-8")

        run = FakeRunner(
            {("git", "remote", "get-url"): CommandResult(
                0, "https://github.com/org/CI-Portal.git\n"),
             ("gh", "repo", "view"): CommandResult(0, "dev\n"),
             ("gh", "pr", "create"): CommandResult(0, "url\n")},
            on_call=seed,
        )
        result = ship_repo(repo, Policy(keep=["CI-Portal/deploy.yml"]), wt_root,
                           retention_days=7, run=run, apply=True)
        assert result.status == "shipped"
        assert result.retention == ["deploy.yml"]

    def test_pr_body_describes_both_changes(self):
        body = pr_body("CI-Hub", ["ci.yml"], 31.48, ["desktop-build.yml"])
        assert "Gates 1 workflow(s)" in body
        assert "Caps artifact `retention-days` on 1 workflow(s)" in body
        assert "## Gated workflows" in body
        assert "## Retention capped" in body

    def test_commit_message_mentions_retention(self):
        msg = commit_message(["ci.yml"], ["build.yml"])
        assert "Capped artifact retention on 1." in msg


def test_push_bypasses_the_pre_push_hook(tmp_path):
    """`hooks install` puts a full lint/test run on every push. Shipping a
    workflow-only commit through it deadlocks - this regressed a live run."""
    repo = tmp_path / "CI-Connect"
    repo.mkdir()
    wt_root = tmp_path / "wt"

    def seed(argv, cwd):
        if argv[:3] == ["git", "worktree", "add"]:
            d = wt_root / "CI-Connect" / ".github" / "workflows"
            d.mkdir(parents=True, exist_ok=True)
            (d / "ci.yml").write_text(MAPPING, encoding="utf-8")

    run = FakeRunner(
        {("git", "remote", "get-url"): CommandResult(
            0, "https://github.com/org/CI-Connect.git\n"),
         ("gh", "repo", "view"): CommandResult(0, "main\n"),
         ("gh", "pr", "create"): CommandResult(0, "url\n")},
        on_call=seed,
    )
    ship_repo(repo, Policy(), wt_root, run=run, apply=True)
    push = run.find("git", "push")
    assert "--no-verify" in push


def test_commit_bypasses_the_pre_commit_hook(tmp_path):
    """A fresh worktree has no node_modules, so a husky/lint-staged pre-commit
    hook fails outright - this broke CI-Server on a live run."""
    repo = tmp_path / "CI-Server"
    repo.mkdir()
    wt_root = tmp_path / "wt"

    def seed(argv, cwd):
        if argv[:3] == ["git", "worktree", "add"]:
            d = wt_root / "CI-Server" / ".github" / "workflows"
            d.mkdir(parents=True, exist_ok=True)
            (d / "ci.yml").write_text(MAPPING, encoding="utf-8")

    run = FakeRunner(
        {("git", "remote", "get-url"): CommandResult(
            0, "https://github.com/org/CI-Server.git\n"),
         ("gh", "repo", "view"): CommandResult(0, "main\n"),
         ("gh", "pr", "create"): CommandResult(0, "url\n")},
        on_call=seed,
    )
    ship_repo(repo, Policy(), wt_root, run=run, apply=True)
    assert "--no-verify" in run.find("git", "commit")


NO_CONCURRENCY = """\
name: CI
on:
  push:
    branches: [main]
  pull_request:
jobs:
  build:
    runs-on: ubuntu-latest
"""


class TestShipWithConcurrency:
    def test_concurrency_off_by_default(self, tmp_path):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(NO_CONCURRENCY, encoding="utf-8")
        _, _, concurrent = gate_worktree(tmp_path, "X", Policy())
        assert concurrent == []
        assert "concurrency:" not in (wf / "ci.yml").read_text()

    def test_added_to_gated_and_kept_alike(self, tmp_path):
        """A kept deploy workflow still fires automatically, so it is exactly
        the case concurrency is for."""
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(NO_CONCURRENCY, encoding="utf-8")
        (wf / "deploy.yml").write_text(NO_CONCURRENCY, encoding="utf-8")

        policy = Policy(keep=["CI-Portal/deploy.yml"])
        gated, _, concurrent = gate_worktree(tmp_path, "CI-Portal", policy,
                                             concurrency=True)
        assert gated == ["ci.yml"]
        assert concurrent == ["ci.yml", "deploy.yml"]
        # ...and the deploy path queues rather than being interrupted.
        assert "cancel-in-progress: false" in (wf / "deploy.yml").read_text()
        assert "cancel-in-progress: true" in (wf / "ci.yml").read_text()

    def test_commit_subject_switches_when_only_concurrency_changed(self):
        msg = commit_message([], [], ["ci.yml"])
        assert msg.splitlines()[0] == "chore(ci): cancel superseded workflow runs"

    def test_combined_change_keeps_the_gating_subject(self):
        msg = commit_message(["ci.yml"], [], ["ci.yml"])
        assert "gate automatic workflow triggers" in msg.splitlines()[0]
        assert "Added concurrency to 1." in msg

    def test_pr_body_explains_the_deploy_exception(self):
        body = pr_body("CI-Portal", [], 0.0, [], ["deploy.yml"])
        assert "cancel-in-progress: false" in body
        assert "## Concurrency added" in body


class TestGatingCanBeDisabled:
    """A concurrency-only PR must not smuggle trigger gating into a branch
    named for something else."""

    def test_no_gate_leaves_triggers_alone(self, tmp_path):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(NO_CONCURRENCY, encoding="utf-8")

        gated, _, concurrent = gate_worktree(
            tmp_path, "X", Policy(), concurrency=True, gating=False
        )
        assert gated == []
        assert concurrent == ["ci.yml"]
        text = (wf / "ci.yml").read_text()
        assert "ci-local:gated" not in text
        assert "concurrency:" in text

    def test_gating_on_by_default(self, tmp_path):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(NO_CONCURRENCY, encoding="utf-8")
        gated, _, _ = gate_worktree(tmp_path, "X", Policy())
        assert gated == ["ci.yml"]
