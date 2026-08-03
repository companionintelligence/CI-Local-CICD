"""End-to-end CLI behaviour against a throwaway workspace."""

from __future__ import annotations

import json

import pytest

from local_ci.cli import load_usage, main, workflow_files

from .test_workflows import MANUAL_ONLY, MAPPING

USAGE_CSV = """\
"date","product","sku","quantity","unit_type","applied_cost_per_quantity","gross_amount","discount_amount","net_amount","username","organization","repository","workflow_path","cost_center_name"
"2026-07-01","actions","actions_linux","100","minutes","0.006","0.60","0.0","0.60","u","org","CI-Server",".github/workflows/ci.yml",""
"2026-07-02","actions","actions_linux","900","minutes","0.006","5.40","0.0","5.40","u","org","CI-Server",".github/workflows/ci.yml",""
"2026-07-02","actions","actions_macos","10","minutes","0.062","0.62","0.0","0.62","u","org","CI-Hub",".github/workflows/desktop-build.yml",""
"2026-07-03","copilot","copilot_ai_credit","10","ai-credits","0.01","0.10","0.10","0","u","org","","",""
"""


@pytest.fixture
def workspace(tmp_path):
    """Two repos: one gateable CI workflow, one already-manual release."""
    server = tmp_path / "CI-Server" / ".github" / "workflows"
    server.mkdir(parents=True)
    (tmp_path / "CI-Server" / ".git").mkdir()
    (server / "ci.yml").write_text(MAPPING, encoding="utf-8")

    hub = tmp_path / "CI-Hub" / ".github" / "workflows"
    hub.mkdir(parents=True)
    (tmp_path / "CI-Hub" / ".git").mkdir()
    (hub / "desktop-build.yml").write_text(MAPPING, encoding="utf-8")
    (hub / "desktop-release.yml").write_text(MANUAL_ONLY, encoding="utf-8")
    return tmp_path


@pytest.fixture
def policy_file(tmp_path):
    path = tmp_path / "policy.toml"
    path.write_text(
        '[workflows]\nkeep = ["CI-Hub/desktop-build.yml"]\nforce_gate = []\n',
        encoding="utf-8",
    )
    return path


def run(workspace, policy_file, *args):
    return main([
        "--workspace", str(workspace), "--policy", str(policy_file), *args
    ])


class TestUsageParsing:
    def test_sums_net_per_repo_and_workflow(self, tmp_path):
        csv_path = tmp_path / "usage.csv"
        csv_path.write_text(USAGE_CSV, encoding="utf-8")
        usage = load_usage(csv_path)
        assert usage[("CI-Server", "ci.yml")] == pytest.approx(6.00)
        assert usage[("CI-Hub", "desktop-build.yml")] == pytest.approx(0.62)

    def test_ignores_non_actions_products(self, tmp_path):
        csv_path = tmp_path / "usage.csv"
        csv_path.write_text(USAGE_CSV, encoding="utf-8")
        assert all(repo for repo, _ in load_usage(csv_path))


class TestWorkflowDiscovery:
    def test_finds_yml_and_yaml_only(self, workspace):
        wf_dir = workspace / "CI-Server" / ".github" / "workflows"
        (wf_dir / "notes.md").write_text("ignore me", encoding="utf-8")
        (wf_dir / "extra.yaml").write_text(MAPPING, encoding="utf-8")
        names = [p.name for p in workflow_files(workspace / "CI-Server")]
        assert names == ["ci.yml", "extra.yaml"]

    def test_repo_without_workflows(self, tmp_path):
        (tmp_path / "bare").mkdir()
        assert workflow_files(tmp_path / "bare") == []


class TestAudit:
    def test_reports_each_bucket(self, workspace, policy_file, capsys):
        assert run(workspace, policy_file, "audit") == 0
        out = capsys.readouterr().out
        assert "GATE (1)" in out
        assert "CI-Server/ci.yml" in out
        assert "KEEP (1)" in out
        assert "ALREADY-MANUAL (1)" in out

    def test_attaches_cost_when_given_a_usage_report(
        self, workspace, policy_file, tmp_path, capsys
    ):
        csv_path = tmp_path / "usage.csv"
        csv_path.write_text(USAGE_CSV, encoding="utf-8")
        run(workspace, policy_file, "audit", "--usage", str(csv_path))
        out = capsys.readouterr().out
        assert "$   6.00" in out
        assert "estimated monthly reduction from gating: $6.00" in out


class TestGateAndRestore:
    def test_dry_run_writes_nothing(self, workspace, policy_file, capsys):
        original = (workspace / "CI-Server/.github/workflows/ci.yml").read_text()
        assert run(workspace, policy_file, "gate") == 0
        assert "--apply" in capsys.readouterr().out
        after = (workspace / "CI-Server/.github/workflows/ci.yml").read_text()
        assert after == original

    def test_apply_gates_only_the_undecided_workflow(self, workspace, policy_file):
        run(workspace, policy_file, "gate", "--apply")
        gated = (workspace / "CI-Server/.github/workflows/ci.yml").read_text()
        kept = (workspace / "CI-Hub/.github/workflows/desktop-build.yml").read_text()
        assert "ci-local:gated" in gated
        assert "workflow_dispatch" in gated
        assert kept == MAPPING  # protected by the keep list

    def test_restore_undoes_a_gate(self, workspace, policy_file):
        path = workspace / "CI-Server/.github/workflows/ci.yml"
        run(workspace, policy_file, "gate", "--apply")
        run(workspace, policy_file, "restore", "--apply")
        assert path.read_text(encoding="utf-8") == MAPPING

    def test_repo_filter_limits_scope(self, workspace, policy_file):
        run(workspace, policy_file, "--repo", "CI-Hub", "gate", "--apply")
        untouched = (workspace / "CI-Server/.github/workflows/ci.yml").read_text()
        assert untouched == MAPPING

    def test_gate_is_safe_to_run_twice(self, workspace, policy_file, capsys):
        run(workspace, policy_file, "gate", "--apply")
        capsys.readouterr()
        run(workspace, policy_file, "gate", "--apply")
        assert "nothing to gate" in capsys.readouterr().out


class TestRunCommand:
    def test_reports_and_writes_json(self, tmp_path, policy_file, capsys):
        repo = tmp_path / "CI-Server"
        (repo / ".git").mkdir(parents=True)
        (repo / "package.json").write_text(
            json.dumps({"name": "s", "scripts": {"test": "exit 0"}}),
            encoding="utf-8",
        )
        (repo / "package-lock.json").write_text("{}", encoding="utf-8")
        (repo / "node_modules").mkdir()
        report = tmp_path / "report.json"
        main([
            "--workspace", str(tmp_path), "--policy", str(policy_file),
            "run", "--jobs", "1", "--report", str(report),
        ])
        capsys.readouterr()
        assert json.loads(report.read_text(encoding="utf-8"))["repos"] == 1

    def test_no_runnable_repos_is_an_error(self, tmp_path, policy_file, capsys):
        (tmp_path / "empty" / ".git").mkdir(parents=True)
        code = main([
            "--workspace", str(tmp_path), "--policy", str(policy_file), "run",
        ])
        assert code == 1
        assert "no runnable repos" in capsys.readouterr().err


class TestHooksCommand:
    def test_install_then_uninstall(self, tmp_path, policy_file, capsys):
        repo = tmp_path / "CI-Server"
        (repo / ".git" / "hooks").mkdir(parents=True)
        (repo / "package.json").write_text(
            json.dumps({"name": "s", "scripts": {"test": "exit 0"}}),
            encoding="utf-8",
        )
        args = ["--workspace", str(tmp_path), "--policy", str(policy_file), "hooks"]
        main(args + ["install"])
        hook = repo / ".git" / "hooks" / "pre-push"
        assert hook.exists()
        main(args + ["uninstall"])
        assert not hook.exists()

    def test_repos_without_checks_are_skipped(self, tmp_path, policy_file, capsys):
        repo = tmp_path / "Docs"
        (repo / ".git" / "hooks").mkdir(parents=True)
        main([
            "--workspace", str(tmp_path), "--policy", str(policy_file),
            "hooks", "install",
        ])
        assert "no local checks detected" in capsys.readouterr().out
        assert not (repo / ".git" / "hooks" / "pre-push").exists()


class TestCostAttribution:
    def test_alias_resolves_a_renamed_repo(self, tmp_path, capsys):
        """A clone dir that differs from the GitHub name must still show cost."""
        repo = tmp_path / "CI-Applet" / ".github" / "workflows"
        repo.mkdir(parents=True)
        (tmp_path / "CI-Applet" / ".git").mkdir()
        (repo / "ci.yml").write_text(MAPPING, encoding="utf-8")

        csv_path = tmp_path / "usage.csv"
        csv_path.write_text(
            USAGE_CSV.replace('"CI-Server",".github/workflows/ci.yml"',
                              '"CI-Capture",".github/workflows/ci.yml"'),
            encoding="utf-8",
        )
        policy = tmp_path / "p.toml"
        policy.write_text(
            '[workflows]\nkeep = []\n\n[repos]\n'
            'aliases = { CI-Applet = "CI-Capture" }\n',
            encoding="utf-8",
        )
        main(["--workspace", str(tmp_path), "--policy", str(policy),
              "audit", "--usage", str(csv_path)])
        out = capsys.readouterr().out
        assert "$   6.00  CI-Applet/ci.yml" in out
        assert "estimated monthly reduction from gating: $6.00" in out

    def test_billed_repo_without_a_clone_is_surfaced(
        self, workspace, policy_file, tmp_path, capsys
    ):
        csv_path = tmp_path / "usage.csv"
        csv_path.write_text(
            USAGE_CSV + '"2026-07-04","actions","actions_linux","500","minutes",'
            '"0.006","3.00","0.0","3.00","u","org","CI-Marketplace",'
            '".github/workflows/deploy.yml",""\n',
            encoding="utf-8",
        )
        run(workspace, policy_file, "audit", "--usage", str(csv_path))
        out = capsys.readouterr().out
        assert "billed repos with no local clone" in out
        assert "CI-Marketplace" in out
