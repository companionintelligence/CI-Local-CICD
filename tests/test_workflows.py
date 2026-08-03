"""Trigger-block surgery: parsing, gating, and restoring."""

from __future__ import annotations

import pytest

from local_ci.workflows import (
    WorkflowParseError,
    child_entries,
    declared_triggers,
    find_on_block,
    gate,
    is_gated,
    restore,
)

MAPPING = """\
name: CI

on:
  push:
    branches:
      - main
  pull_request:
    branches:
      - main

concurrency:
  group: ci-${{ github.ref }}

jobs:
  build:
    runs-on: ubuntu-latest
"""

INLINE_SCALAR = """\
name: Scalar
on: push
jobs:
  build:
    runs-on: ubuntu-latest
"""

INLINE_LIST = """\
name: List
on: [push, pull_request]
jobs:
  build:
    runs-on: ubuntu-latest
"""

BLOCK_SEQUENCE = """\
name: Seq
on:
  - push
  - pull_request
jobs:
  build:
    runs-on: ubuntu-latest
"""

QUOTED_KEY = """\
name: Quoted
'on':
  push:
    branches: [main]
jobs:
  build:
    runs-on: ubuntu-latest
"""

WITH_DISPATCH_INPUTS = """\
name: Desktop
on:
  workflow_dispatch:
    inputs:
      tag:
        description: 'Release tag'
        required: true
        type: string
  push:
    branches:
      - dev
jobs:
  build:
    runs-on: macos-latest
"""

REUSABLE = """\
name: Reusable
on:
  workflow_call:
    inputs:
      target:
        type: string
  push:
    branches: [main]
jobs:
  build:
    runs-on: ubuntu-latest
"""

MANUAL_ONLY = """\
name: Manual
on:
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-latest
"""

NO_TRIGGERS = """\
name: Composite
runs:
  using: composite
"""

ALL_SHAPES = [MAPPING, INLINE_SCALAR, INLINE_LIST, BLOCK_SEQUENCE, QUOTED_KEY,
              WITH_DISPATCH_INPUTS, REUSABLE]


class TestFindOnBlock:
    def test_mapping_block_stops_at_next_top_level_key(self):
        block = find_on_block(MAPPING)
        assert not block.inline
        assert block.lines[0] == "on:"
        assert block.lines[-1] == "    branches:\n      - main".splitlines()[-1]
        assert "concurrency:" not in block.text

    def test_inline_scalar(self):
        block = find_on_block(INLINE_SCALAR)
        assert block.inline
        assert block.lines == ["on: push"]

    def test_inline_list(self):
        block = find_on_block(INLINE_LIST)
        assert block.inline
        assert block.lines == ["on: [push, pull_request]"]

    def test_quoted_key(self):
        block = find_on_block(QUOTED_KEY)
        assert not block.inline
        assert block.lines[0] == "'on':"

    def test_missing_on_key_raises(self):
        with pytest.raises(WorkflowParseError):
            find_on_block(NO_TRIGGERS)

    def test_trailing_blank_lines_stay_outside_the_block(self):
        block = find_on_block(MAPPING)
        assert block.lines[-1].strip()


class TestDeclaredTriggers:
    @pytest.mark.parametrize(
        "text,expected",
        [
            (MAPPING, ["push", "pull_request"]),
            (INLINE_SCALAR, ["push"]),
            (INLINE_LIST, ["push", "pull_request"]),
            (BLOCK_SEQUENCE, ["push", "pull_request"]),
            (QUOTED_KEY, ["push"]),
            (REUSABLE, ["workflow_call", "push"]),
            (MANUAL_ONLY, ["workflow_dispatch"]),
            (NO_TRIGGERS, []),
        ],
    )
    def test_triggers(self, text, expected):
        assert declared_triggers(text) == expected

    def test_child_entries_capture_nested_config(self):
        entries = child_entries(find_on_block(WITH_DISPATCH_INPUTS))
        assert set(entries) == {"workflow_dispatch", "push"}
        assert "      tag:" in entries["workflow_dispatch"]
        assert "        required: true" in entries["workflow_dispatch"]
        # The push entry must not swallow the following trigger's lines.
        assert all("tag:" not in line for line in entries["push"])

    def test_child_entries_empty_for_sequence_style(self):
        assert child_entries(find_on_block(BLOCK_SEQUENCE)) == {}


class TestGate:
    def test_replaces_triggers_with_dispatch(self):
        out, changed = gate(MAPPING)
        assert changed
        assert is_gated(out)
        assert declared_triggers(out) == ["workflow_dispatch"]

    def test_preserves_the_rest_of_the_file(self):
        out, _ = gate(MAPPING)
        assert "concurrency:" in out
        assert "  group: ci-${{ github.ref }}" in out
        assert "    runs-on: ubuntu-latest" in out
        assert out.startswith("name: CI\n")

    def test_original_triggers_survive_as_comments(self):
        out, _ = gate(MAPPING)
        assert "#   push:" in out
        assert "#       - main" in out

    def test_workflow_dispatch_inputs_are_carried_over(self):
        out, changed = gate(WITH_DISPATCH_INPUTS)
        assert changed
        assert "      tag:" in out.split("# <<< ci-local:gated")[1]
        assert "        description: 'Release tag'" in out

    def test_workflow_call_is_preserved(self):
        out, changed = gate(REUSABLE)
        assert changed
        triggers = declared_triggers(out)
        assert "workflow_call" in triggers
        assert "push" not in triggers
        assert "      target:" in out.split("# <<< ci-local:gated")[1]

    def test_manual_only_workflow_is_untouched(self):
        out, changed = gate(MANUAL_ONLY)
        assert not changed
        assert out == MANUAL_ONLY

    def test_gating_is_idempotent(self):
        once, _ = gate(MAPPING)
        twice, changed = gate(once)
        assert not changed
        assert twice == once

    def test_missing_on_key_raises(self):
        with pytest.raises(WorkflowParseError):
            gate(NO_TRIGGERS)


class TestRestore:
    @pytest.mark.parametrize("text", ALL_SHAPES)
    def test_round_trip_is_byte_identical(self, text):
        gated, changed = gate(text)
        assert changed
        back, restored = restore(gated)
        assert restored
        assert back == text

    def test_round_trip_without_trailing_newline(self):
        text = MAPPING.rstrip("\n")
        gated, _ = gate(text)
        assert not gated.endswith("\n")
        back, _ = restore(gated)
        assert back == text

    def test_restore_on_ungated_file_is_a_no_op(self):
        back, changed = restore(MAPPING)
        assert not changed
        assert back == MAPPING

    def test_restore_preserves_trailing_whitespace_in_triggers(self):
        text = "name: X\non:\n  push:   \n    branches: [main]\njobs:\n  a:\n"
        gated, _ = gate(text)
        back, _ = restore(gated)
        assert back == text


CRLF = "name: X\r\non:\r\n  push:\r\n    branches: [main]\r\njobs:\r\n  a: 1\r\n"
TRAILING_BLANK = "name: X\non:\n  push:\n    branches: [main]\njobs:\n  a: 1\n\n"


class TestExactFileFidelity:
    """Regressions from gating the real workspace: both silently rewrote files
    that were supposed to be untouched outside the `on:` block."""

    def test_crlf_line_endings_survive_a_round_trip(self):
        gated, changed = gate(CRLF)
        assert changed
        back, _ = restore(gated)
        assert back == CRLF
        assert "\r\n" in back

    def test_crlf_file_is_not_converted_by_gating(self):
        gated, _ = gate(CRLF)
        # Every original line keeps its CR; a stray LF-only line means the file
        # was reflowed and the diff will cover the whole file.
        assert "jobs:\r\n" in gated

    def test_trailing_blank_line_survives_restore(self):
        gated, _ = gate(TRAILING_BLANK)
        assert gated.endswith("a: 1\n\n")
        back, _ = restore(gated)
        assert back == TRAILING_BLANK

    def test_several_trailing_blank_lines_survive(self):
        text = TRAILING_BLANK + "\n\n"
        gated, _ = gate(text)
        back, _ = restore(gated)
        assert back == text


class TestFileIO:
    def test_read_workflow_does_not_translate_newlines(self, tmp_path):
        from local_ci.workflows import read_workflow, write_workflow

        path = tmp_path / "wf.yml"
        path.write_bytes(CRLF.encode("utf-8"))
        assert read_workflow(path) == CRLF

        write_workflow(path, CRLF)
        assert path.read_bytes() == CRLF.encode("utf-8")

    def test_round_trip_through_disk_is_byte_identical(self, tmp_path):
        from local_ci.workflows import read_workflow, write_workflow

        path = tmp_path / "wf.yml"
        original = CRLF.encode("utf-8")
        path.write_bytes(original)

        gated, _ = gate(read_workflow(path))
        write_workflow(path, gated)
        back, _ = restore(read_workflow(path))
        write_workflow(path, back)
        assert path.read_bytes() == original
