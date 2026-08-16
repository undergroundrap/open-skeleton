# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Reading two keys out of YAML without a YAML parser.

The distribution declares no runtime dependencies, so there is none to use.
That is affordable only because the target is narrow -- `on:` and `jobs:` at
the top level -- and it stays honest only if every form this cannot read
produces nothing rather than a guess. Each case here is a spelling GitHub
accepts, and the last of them is the one that must stay silent.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.workflow_triggers import ANALYZER_VERSION, read_workflow
from open_skeleton.scanner import scan_repository


class ReadWorkflowTests(TestCase):
    def test_a_block_mapping_names_each_trigger_once(self) -> None:
        text = "on:\n  push:\n    branches: [main]\n  pull_request:\njobs:\n  test:\n  lint:\n"
        events, jobs = read_workflow(text)
        # `branches` is a filter on `push`, not a third trigger.
        self.assertEqual(events, ("push", "pull_request"))
        self.assertEqual(jobs, ("test", "lint"))

    def test_an_inline_list_is_read(self) -> None:
        events, _ = read_workflow("on: [push, pull_request]\njobs:\n  a:\n")
        self.assertEqual(events, ("push", "pull_request"))

    def test_a_scalar_is_read(self) -> None:
        events, _ = read_workflow("on: push\njobs:\n  a:\n")
        self.assertEqual(events, ("push",))

    def test_a_quoted_key_is_read(self) -> None:
        # `on` is a YAML 1.1 boolean, so a careful author writes `"on":`. A
        # reader accepting only bare keys finds no triggers in exactly the
        # files whose authors were careful.
        events, _ = read_workflow('"on":\n  schedule:\n    - cron: "0 0 * * *"\njobs:\n  n:\n')
        self.assertEqual(events, ("schedule",))

    def test_a_trailing_comment_is_not_part_of_the_value(self) -> None:
        events, _ = read_workflow("on:  # when this runs\n  push:\njobs:\n  a:\n")
        self.assertEqual(events, ("push",))

    def test_a_hash_inside_a_quoted_scalar_is_not_a_comment(self) -> None:
        events, _ = read_workflow('on: push\nname: "release #2"\njobs:\n  a:\n')
        self.assertEqual(events, ("push",))

    def test_a_workflow_with_no_trigger_key_reports_nothing(self) -> None:
        # Silence, not an absence claim: this reader not finding `on:` says
        # something about the reader, and a workflow always has a trigger.
        events, jobs = read_workflow("name: x\njobs:\n  a:\n")
        self.assertEqual(events, ())
        self.assertEqual(jobs, ("a",))


class WorkflowClaimTests(TestCase):
    def _claims(self, workflow: str) -> list[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            (root / ".github" / "workflows" / "ci.yml").write_text(workflow, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            return [c.claim for c in result.claims if c.produced_by == ANALYZER_VERSION]

    def test_a_workflow_that_exists_is_described(self) -> None:
        # The engine reported the *absence* of CI and said nothing about its
        # presence, which is the wrong way round: what runs the suite is a
        # question about a repository that has one.
        found = self._claims("on:\n  push:\n    branches: [main]\njobs:\n  test:\n  lint:\n")
        self.assertTrue(any("runs on `push`" in item for item in found))
        self.assertTrue(any("2 job(s)" in item for item in found))

    def test_running_only_on_code_arriving_is_stated(self) -> None:
        found = self._claims("on:\n  push:\njobs:\n  test:\n")
        self.assertTrue(
            any("no scheduled or manually dispatched trigger" in item for item in found)
        )

    def test_a_dispatchable_workflow_is_not_described_that_way(self) -> None:
        found = self._claims("on:\n  push:\n  workflow_dispatch:\njobs:\n  test:\n")
        self.assertTrue(found)
        self.assertFalse(any("no scheduled or manually" in item for item in found))

    def test_an_unreadable_workflow_produces_no_claim(self) -> None:
        self.assertEqual(self._claims("name: broken\njobs:\n  a:\n"), [])
