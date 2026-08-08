"""Runbook generation."""

from __future__ import annotations

import pytest

from local_ci.detect import Check, RepoPlan
from local_ci.docs import dispatch_command, is_release, render, render_repo
from local_ci.verify import DispatchInput, RemoteWorkflow, RepoAudit


def wf(name, gated=True, triggers=("workflow_dispatch",), title="", inputs=(),
       kept=None):
    return RemoteWorkflow(name=name, gated=gated, triggers=list(triggers),
                          kept=kept, title=title or name, inputs=list(inputs))


def plan(repo="CI-Server"):
    return RepoPlan(
        repo=repo, path=None, stack="node", manager="yarn",
        install=Check("install", ["yarn", "install", "--immutable"]),
        checks=[Check("lint", ["yarn", "lint"]),
                Check("test", ["yarn", "test"]),
                Check("build", ["yarn", "build"], required=False)],
    )


class TestReleaseDetection:
    @pytest.mark.parametrize("name", [
        "release.yml", "docker-publish.yml", "deploy-site.yml",
        "pages.yml", "semver-tag.yml", "nightly-release.yml",
    ])
    def test_release_paths(self, name):
        assert is_release(wf(name))

    @pytest.mark.parametrize("name", ["ci.yml", "e2e.yml", "codeql.yml",
                                      "integration-tests.yml"])
    def test_check_paths(self, name):
        assert not is_release(wf(name))


class TestDispatchCommand:
    def test_basic_command(self):
        cmd = dispatch_command("CI-Server", wf("ci.yml"), "main")
        assert cmd == ("gh workflow run ci.yml "
                       "--repo companionintelligence/CI-Server --ref main")

    def test_uses_the_real_default_branch(self):
        cmd = dispatch_command("CI-Hub", wf("ci.yml"), "dev")
        assert "--ref dev" in cmd
        assert "--ref main" not in cmd

    def test_includes_input_flags(self):
        w = wf("release.yml", inputs=[
            DispatchInput("tag", required=True, type="string"),
            DispatchInput("dry_run", default="true", type="boolean"),
        ])
        cmd = dispatch_command("CI-Hub", w, "dev")
        assert "-f tag=<string>" in cmd
        assert "-f dry_run=true" in cmd

    def test_slug_can_differ_from_clone_name(self):
        """CI-Applet's clone directory does not match its GitHub name."""
        cmd = dispatch_command("CI-Capture", wf("ci.yml"), "main")
        assert "companionintelligence/CI-Capture" in cmd


class TestRenderRepo:
    def test_includes_local_commands(self):
        audit = RepoAudit("CI-Server", "main", workflows=[wf("ci.yml")])
        out = "\n".join(render_repo(audit, plan(), "CI-Server"))
        assert "ci-local run --repo CI-Server" in out
        assert "yarn lint" in out
        assert "yarn install --immutable" in out

    def test_optional_checks_are_marked(self):
        audit = RepoAudit("CI-Server", "main", workflows=[wf("ci.yml")])
        out = "\n".join(render_repo(audit, plan(), "CI-Server"))
        assert "yarn build   # optional" in out

    def test_separates_checks_from_releases(self):
        audit = RepoAudit("CI-Hub", "dev", workflows=[
            wf("ci.yml", title="Hub CI"),
            wf("desktop-release.yml", title="Desktop Release"),
        ])
        out = "\n".join(render_repo(audit, None, "CI-Hub"))
        assert "### Run a check workflow on GitHub" in out
        assert "### Cut a release" in out
        assert out.index("Hub CI") < out.index("### Cut a release")

    def test_required_inputs_are_called_out(self):
        audit = RepoAudit("CI-Hub", "dev", workflows=[
            wf("release.yml", inputs=[
                DispatchInput("tag", required=True, description="Release tag"),
                DispatchInput("draft", default="false"),
            ]),
        ])
        out = "\n".join(render_repo(audit, None, "CI-Hub"))
        assert "Required inputs: `tag` (Release tag)" in out
        assert "draft" not in out.split("Required inputs")[1].split("\n")[0]

    def test_automatic_workflows_listed_separately(self):
        audit = RepoAudit("CI-Portal", "dev", workflows=[
            wf("ci.yml"),
            wf("deploy.yml", gated=False, triggers=["push"], kept="*/deploy.yml"),
        ])
        out = "\n".join(render_repo(audit, None, "CI-Portal"))
        assert "### Still automatic" in out
        assert "- `deploy.yml` on push" in out

    def test_repo_without_local_checks(self):
        audit = RepoAudit("CI-Docs", "main", workflows=[wf("ci.yml")])
        out = "\n".join(render_repo(audit, None, "CI-Docs"))
        assert "No local checks detected" in out

    def test_errored_repo_is_reported_not_skipped(self):
        audit = RepoAudit("Broken", error="cannot resolve default branch")
        out = "\n".join(render_repo(audit, None, "Broken"))
        assert "Could not read workflows" in out

    def test_default_branch_is_shown(self):
        audit = RepoAudit("CI-Hub", "dev", workflows=[wf("ci.yml")])
        out = "\n".join(render_repo(audit, None, "CI-Hub"))
        assert "Default branch: `dev`" in out


class TestRender:
    def test_repos_are_alphabetical(self):
        audits = [
            RepoAudit("Zulu", "main", workflows=[wf("ci.yml")]),
            RepoAudit("alpha", "main", workflows=[wf("ci.yml")]),
            RepoAudit("Mike", "main", workflows=[wf("ci.yml")]),
        ]
        out = render(audits, {}, {})
        assert out.index("## alpha") < out.index("## Mike") < out.index("## Zulu")

    def test_repos_without_workflows_are_omitted(self):
        audits = [RepoAudit("Empty", "main", workflows=[]),
                  RepoAudit("Real", "main", workflows=[wf("ci.yml")])]
        out = render(audits, {}, {})
        assert "## Real" in out
        assert "## Empty" not in out

    def test_header_explains_the_ref_gotcha(self):
        out = render([RepoAudit("X", "main", workflows=[wf("ci.yml")])], {}, {})
        assert "--ref" in out
        assert "ci-local run" in out

    def test_says_it_is_generated(self):
        out = render([RepoAudit("X", "main", workflows=[wf("ci.yml")])], {}, {})
        assert "ci-local docs" in out
        assert "Do not hand-edit" in out

    def test_ends_with_single_newline(self):
        out = render([RepoAudit("X", "main", workflows=[wf("ci.yml")])], {}, {})
        assert out.endswith("\n")
        assert not out.endswith("\n\n")


class TestRepoDoc:
    def test_is_self_contained(self):
        """A pointer into another private repo is not documentation."""
        from local_ci.docs import render_repo_doc

        audit = RepoAudit("CI-Hub", "dev", workflows=[
            wf("ci.yml", title="Hub CI"),
            wf("release.yml", title="Release",
               inputs=[DispatchInput("tag", required=True)]),
        ])
        out = render_repo_doc(audit, plan("CI-Hub"), "CI-Hub")
        assert out.startswith("# Running CI for this repo")
        assert "gh workflow run ci.yml" in out
        assert "gh workflow run release.yml" in out
        assert "-f tag=" in out
        assert "ci-local run --repo CI-Hub" in out

    def test_does_not_repeat_the_repo_heading(self):
        from local_ci.docs import render_repo_doc

        audit = RepoAudit("CI-Hub", "dev", workflows=[wf("ci.yml")])
        out = render_repo_doc(audit, None, "CI-Hub")
        assert "## CI-Hub" not in out
        assert "Default branch: `dev`" in out

    def test_explains_why_and_says_generated(self):
        from local_ci.docs import render_repo_doc

        audit = RepoAudit("X", "main", workflows=[wf("ci.yml")])
        out = render_repo_doc(audit, None, "X")
        assert "gated" in out
        assert "do not hand-edit" in out.lower()
        assert "--ref" in out

    def test_ends_with_single_newline(self):
        from local_ci.docs import render_repo_doc

        audit = RepoAudit("X", "main", workflows=[wf("ci.yml")])
        out = render_repo_doc(audit, None, "X")
        assert out.endswith("\n") and not out.endswith("\n\n")
