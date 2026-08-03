"""Capping artifact retention on upload-artifact steps."""

from __future__ import annotations

import pytest
import yaml

from local_ci.retention import apply_retention

WITH_BLOCK = """\
name: Build
on:
  push:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Upload bundle
        uses: actions/upload-artifact@v4
        with:
          name: bundle
          path: dist/
      - name: Done
        run: echo ok
"""

NO_WITH = """\
name: Build
on:
  push:
jobs:
  build:
    steps:
      - uses: actions/upload-artifact@v4
      - run: echo after
"""

ALREADY_LOW = """\
name: Build
on:
  push:
jobs:
  build:
    steps:
      - uses: actions/upload-artifact@v4
        with:
          path: dist/
          retention-days: 3
"""

TOO_HIGH = """\
name: Build
on:
  push:
jobs:
  build:
    steps:
      - uses: actions/upload-artifact@v4
        with:
          path: dist/
          retention-days: 90
"""

MULTIPLE = """\
name: Build
on:
  push:
jobs:
  build:
    steps:
      - name: One
        uses: actions/upload-artifact@v4
        with:
          path: a/
      - name: Two
        uses: actions/upload-artifact@v4
        with:
          path: b/
      - name: Three
        uses: actions/upload-artifact@v4
        with:
          path: c/
"""

NO_UPLOADS = """\
name: Test
on:
  push:
jobs:
  test:
    steps:
      - uses: actions/checkout@v4
      - run: npm test
"""


def retention_values(text):
    doc = yaml.safe_load(text)
    found = []
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if "upload-artifact" in str(step.get("uses", "")):
                found.append((step.get("with") or {}).get("retention-days"))
    return found


class TestAddingRetention:
    def test_inserts_into_an_existing_with_block(self):
        out, changes = apply_retention(WITH_BLOCK, days=7)
        assert [c.action for c in changes] == ["added"]
        assert retention_values(out) == [7]
        # Existing keys survive.
        assert "          name: bundle" in out
        assert "          path: dist/" in out

    def test_creates_a_with_block_when_absent(self):
        out, changes = apply_retention(NO_WITH, days=7)
        assert [c.action for c in changes] == ["added-with"]
        assert retention_values(out) == [7]
        assert "      - run: echo after" in out

    def test_matches_surrounding_indentation(self):
        out, _ = apply_retention(WITH_BLOCK, days=7)
        assert "          retention-days: 7" in out

    def test_all_uploads_in_a_file_are_covered(self):
        out, changes = apply_retention(MULTIPLE, days=7)
        assert len(changes) == 3
        assert retention_values(out) == [7, 7, 7]

    def test_steps_after_an_edit_are_not_corrupted(self):
        """Inserting a line shifts later indices - they must still resolve."""
        out, _ = apply_retention(MULTIPLE, days=7)
        doc = yaml.safe_load(out)
        paths = [
            s["with"]["path"] for s in doc["jobs"]["build"]["steps"]
        ]
        assert paths == ["a/", "b/", "c/"]


class TestExistingValues:
    def test_value_above_the_cap_is_lowered(self):
        out, changes = apply_retention(TOO_HIGH, days=7)
        assert [c.action for c in changes] == ["lowered"]
        assert "90 -> 7 days" in changes[0].detail
        assert retention_values(out) == [7]

    def test_value_below_the_cap_is_left_alone(self):
        out, changes = apply_retention(ALREADY_LOW, days=7)
        assert changes == []
        assert out == ALREADY_LOW

    def test_value_equal_to_the_cap_is_left_alone(self):
        text = TOO_HIGH.replace("retention-days: 90", "retention-days: 7")
        out, changes = apply_retention(text, days=7)
        assert changes == []
        assert out == text


class TestNoOps:
    def test_workflow_without_uploads_is_untouched(self):
        out, changes = apply_retention(NO_UPLOADS, days=7)
        assert changes == []
        assert out == NO_UPLOADS

    def test_is_idempotent(self):
        once, _ = apply_retention(WITH_BLOCK, days=7)
        twice, changes = apply_retention(once, days=7)
        assert changes == []
        assert twice == once

    def test_output_stays_valid_yaml(self):
        for text in (WITH_BLOCK, NO_WITH, TOO_HIGH, MULTIPLE):
            out, _ = apply_retention(text, days=7)
            assert yaml.safe_load(out) is not None

    def test_trailing_newline_preserved(self):
        out, _ = apply_retention(WITH_BLOCK, days=7)
        assert out.endswith("\n")


@pytest.mark.parametrize("days", [1, 5, 14, 30])
def test_configurable_cap(days):
    out, _ = apply_retention(NO_WITH, days=days)
    assert retention_values(out) == [days]
