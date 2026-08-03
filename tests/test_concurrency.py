"""Inserting a top-level concurrency block."""

from __future__ import annotations

import pytest
import yaml

from local_ci.concurrency import GROUP, add_concurrency, has_concurrency
from local_ci.workflows import gate

PLAIN = """\
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  build:
    runs-on: ubuntu-latest
"""

WITH_PERMISSIONS = """\
name: CI
on:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
"""

ALREADY = """\
name: CI
on:
  push:
concurrency:
  group: custom
  cancel-in-progress: true
jobs:
  build:
    runs-on: ubuntu-latest
"""

INLINE_ON = """\
name: CI
on: [push, pull_request]
jobs:
  build:
    runs-on: ubuntu-latest
"""

NO_TRIGGERS = """\
name: Composite
runs:
  using: composite
"""


def parse(text):
    return yaml.safe_load(text)


def triggers_of(doc):
    """The `on:` key, worked around.

    YAML 1.1 resolves the bare token `on` to boolean True, so PyYAML returns
    the trigger mapping under the key True rather than "on". Every GitHub
    workflow hits this.
    """
    if "on" in doc:
        return doc["on"]
    return doc[True]


class TestInsertion:
    def test_adds_block_with_ref_scoped_group(self):
        out, change = add_concurrency(PLAIN, "ci.yml")
        assert change is not None
        doc = parse(out)
        assert doc["concurrency"]["group"] == GROUP
        assert doc["concurrency"]["cancel-in-progress"] is True

    def test_group_includes_the_ref(self):
        """Without the ref, every open PR cancels every other one."""
        out, _ = add_concurrency(PLAIN, "ci.yml")
        assert "github.ref" in parse(out)["concurrency"]["group"]

    def test_sits_above_jobs(self):
        out, _ = add_concurrency(PLAIN, "ci.yml")
        assert out.index("concurrency:") < out.index("jobs:")

    def test_sits_below_the_trigger_block(self):
        out, _ = add_concurrency(PLAIN, "ci.yml")
        assert out.index("on:") < out.index("concurrency:")

    def test_does_not_land_inside_the_on_block(self):
        out, _ = add_concurrency(PLAIN, "ci.yml")
        doc = parse(out)
        assert set(triggers_of(doc)) == {"push", "pull_request"}
        assert "concurrency" not in triggers_of(doc)

    def test_inserts_before_permissions(self):
        out, _ = add_concurrency(WITH_PERMISSIONS, "ci.yml")
        assert out.index("concurrency:") < out.index("permissions:")
        assert parse(out)["permissions"]["contents"] == "read"

    def test_works_with_inline_trigger_list(self):
        out, change = add_concurrency(INLINE_ON, "ci.yml")
        assert change is not None
        assert parse(out)["concurrency"]["group"] == GROUP
        assert triggers_of(parse(out)) == ["push", "pull_request"]

    def test_rest_of_file_is_preserved(self):
        out, _ = add_concurrency(PLAIN, "ci.yml")
        assert "    runs-on: ubuntu-latest" in out
        assert out.startswith("name: CI\n")
        assert out.endswith("\n")


class TestShipPathSafety:
    @pytest.mark.parametrize("name", [
        "deploy.yml", "release.yml", "docker-publish.yml",
        "sdk-release.yml", "pages.yml",
    ])
    def test_ship_paths_queue_instead_of_cancelling(self, name):
        """Cancelling a deploy mid-flight can leave it half-applied."""
        out, change = add_concurrency(PLAIN, name)
        assert change.cancel is False
        assert parse(out)["concurrency"]["cancel-in-progress"] is False

    @pytest.mark.parametrize("name", ["ci.yml", "e2e.yml", "playwright.yml",
                                      "codeql.yml", "integration-tests.yml"])
    def test_test_paths_cancel(self, name):
        out, change = add_concurrency(PLAIN, name)
        assert change.cancel is True
        assert parse(out)["concurrency"]["cancel-in-progress"] is True

    def test_explicit_override_wins(self):
        _, change = add_concurrency(PLAIN, "deploy.yml", cancel=True)
        assert change.cancel is True


class TestNoOps:
    def test_existing_block_is_left_alone(self):
        out, change = add_concurrency(ALREADY, "ci.yml")
        assert change is None
        assert out == ALREADY
        assert parse(out)["concurrency"]["group"] == "custom"

    def test_has_concurrency_detects_top_level_only(self):
        assert has_concurrency(ALREADY)
        assert not has_concurrency(PLAIN)
        # A job-level concurrency key must not count as the top-level one.
        nested = PLAIN.replace("    runs-on: ubuntu-latest",
                               "    concurrency: x\n    runs-on: ubuntu-latest")
        assert not has_concurrency(nested)

    def test_workflow_without_triggers_is_skipped(self):
        out, change = add_concurrency(NO_TRIGGERS, "action.yml")
        assert change is None
        assert out == NO_TRIGGERS

    def test_is_idempotent(self):
        once, _ = add_concurrency(PLAIN, "ci.yml")
        twice, change = add_concurrency(once, "ci.yml")
        assert change is None
        assert twice == once


class TestInteractionWithGating:
    def test_applies_cleanly_to_a_gated_workflow(self):
        """Gating rewrites the `on:` block; concurrency must still land after
        it, not inside the preserved comment header."""
        gated, _ = gate(PLAIN)
        out, change = add_concurrency(gated, "ci.yml")
        assert change is not None
        doc = parse(out)
        assert doc["concurrency"]["group"] == GROUP
        assert list(triggers_of(doc)) == ["workflow_dispatch"]
        # The commented-out original triggers survive untouched.
        assert "#   push:" in out

    def test_order_does_not_matter(self):
        a, _ = add_concurrency(gate(PLAIN)[0], "ci.yml")
        b = gate(add_concurrency(PLAIN, "ci.yml")[0])[0]
        assert parse(a)["concurrency"] == parse(b)["concurrency"]
        assert list(triggers_of(parse(a))) == list(triggers_of(parse(b)))


def test_output_is_valid_yaml_for_every_shape():
    for text in (PLAIN, WITH_PERMISSIONS, INLINE_ON):
        out, _ = add_concurrency(text, "ci.yml")
        assert parse(out) is not None
