"""Runner-aware billing classification, independent of gating."""

from __future__ import annotations

import pytest

from local_ci.billing import (
    LINUX,
    MACOS,
    SELF_HOSTED,
    UNKNOWN,
    WINDOWS,
    classify_billing,
    is_standard_runner,
    runner_platform,
)


class TestRunnerPlatform:
    @pytest.mark.parametrize("label,expected", [
        ("ubuntu-latest", LINUX),
        ("ubuntu-22.04", LINUX),
        ("ubuntu-24.04-arm", LINUX),
        ("windows-latest", WINDOWS),
        ("windows-2022", WINDOWS),
        ("macos-latest", MACOS),
        ("macos-14", MACOS),
        ("self-hosted", SELF_HOSTED),
        ("self-hosted-gpu", SELF_HOSTED),
        ("${{ matrix.os }}", UNKNOWN),
        ("some-org-runner", UNKNOWN),
    ])
    def test_labels(self, label, expected):
        assert runner_platform(label) == expected

    def test_case_insensitive(self):
        assert runner_platform("Ubuntu-Latest") == LINUX

    def test_standard_images_are_recognised(self):
        assert is_standard_runner("ubuntu-latest")
        assert not is_standard_runner("ubuntu-latest-8-cores")
        assert not is_standard_runner("self-hosted")


class TestClassifyBilling:
    def test_manual_only_is_free(self):
        v = classify_billing(["workflow_dispatch"], ["ubuntu-latest"])
        assert not v.billable
        assert v.reason == "no automatic triggers"

    def test_workflow_call_alone_is_free(self):
        assert not classify_billing(["workflow_call"], ["ubuntu-latest"]).billable

    def test_private_linux_bills_at_1x(self):
        v = classify_billing(["push"], ["ubuntu-latest"], private=True)
        assert v.billable
        assert v.multiplier == 1

    def test_windows_bills_at_2x(self):
        assert classify_billing(["push"], ["windows-latest"]).multiplier == 2

    def test_macos_bills_at_10x(self):
        v = classify_billing(["push"], ["macos-latest"])
        assert v.multiplier == 10
        assert "macos" in v.reason

    def test_worst_runner_sets_the_rate(self):
        """A matrix job is priced by its most expensive leg."""
        v = classify_billing(["push"], ["ubuntu-latest", "macos-latest"])
        assert v.multiplier == 10

    def test_public_repo_on_standard_runners_is_free(self):
        v = classify_billing(["push"], ["ubuntu-latest"], private=False)
        assert not v.billable
        assert v.reason == "public repo on standard runners"

    def test_public_repo_on_larger_runner_still_bills(self):
        """Larger runners are billed on public repos too."""
        v = classify_billing(["push"], ["ubuntu-latest-8-cores"], private=False)
        assert v.billable

    def test_self_hosted_is_free_even_when_private(self):
        v = classify_billing(["push", "schedule"], ["self-hosted"], private=True)
        assert not v.billable
        assert v.reason == "self-hosted runners only"

    def test_mixed_self_hosted_and_github_hosted_bills(self):
        """Two separate jobs: one on our metal, one on GitHub's."""
        v = classify_billing(["push"], ["self-hosted", "macos-latest"])
        assert v.billable
        assert v.multiplier == 10

    def test_label_conjunction_naming_an_os_is_still_self_hosted(self):
        """`runs-on: [self-hosted, macos-test]` is ONE self-hosted machine.

        Flattening this group invented a hosted macOS runner and a 10x charge
        on Companion-Pipe/benchmark.yml that does not exist.
        """
        v = classify_billing(["push"], [["self-hosted", "macos-test"]])
        assert not v.billable
        assert v.reason == "self-hosted runners only"

    def test_windows_gpu_conjunction_is_self_hosted(self):
        """CI-XR-Scan-Web-Unity/unity-tests.yml."""
        v = classify_billing(["push"], [["self-hosted", "windows", "x64", "gpu"]])
        assert not v.billable

    def test_grouped_hosted_runner_still_bills(self):
        v = classify_billing(["push"], [["macos-latest"]], private=True)
        assert v.billable
        assert v.multiplier == 10

    def test_unknown_runner_is_assumed_billable(self):
        v = classify_billing(["push"], ["${{ matrix.os }}"])
        assert v.billable
        assert "non-standard" in v.reason

    def test_no_runs_on_is_assumed_billable(self):
        """A caller that only `uses:` a reusable workflow still burns minutes."""
        v = classify_billing(["push"], [])
        assert v.billable
        assert v.reason == "automatic triggers, runner unknown"

    def test_private_defaults_to_true(self):
        """Safe direction: assume it costs money until proven otherwise."""
        assert classify_billing(["push"], ["ubuntu-latest"]).billable

    def test_rate_per_minute_tracks_multiplier(self):
        assert classify_billing(["push"], ["ubuntu-latest"]).rate_per_minute == 0.008
        assert classify_billing(["push"], ["macos-latest"]).rate_per_minute == 0.08
