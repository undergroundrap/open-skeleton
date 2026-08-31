# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from unittest import TestCase

from open_skeleton.providers import LocalCommandProvider, ProviderRequest, ProviderResult
from open_skeleton.synthesis_plan import SYNTHESIS_PLAN_SCHEMA
from open_skeleton.synthesis_runner import request_sha256, run_synthesis_plan


def _plan(path: Path, *, jobs: int = 2) -> None:
    document = {
        "schema": SYNTHESIS_PLAN_SCHEMA,
        "snapshot_id": "snapshot",
        "profile_id": "standard",
        "job_count": jobs,
        "jobs": [
            {
                "job_id": f"section-{index}",
                "task": f"Explain section {index}",
                "parallel_safe": True,
                "context_pack": {
                    "snapshot_id": "snapshot",
                    "query": f"section-{index}",
                    "max_chars": 5_000,
                    "used_chars": 0,
                    "claims": [],
                    "evidence": [],
                    "omitted_claim_ids": [],
                    "omitted_evidence_ids": [],
                    "missing_claim_ids": [],
                    "truncated": False,
                },
            }
            for index in range(jobs)
        ],
    }
    path.write_text(json.dumps(document), encoding="utf-8")


class RecordingProvider:
    name = "local-command"

    def __init__(self) -> None:
        self.calls = 0
        self._lock = Lock()

    def generate(self, request: ProviderRequest, *, workspace: Path) -> ProviderResult:
        self.assert_external_workspace(workspace)
        with self._lock:
            self.calls += 1
        return ProviderResult(
            provider=self.name,
            status="complete",
            snapshot_id=request.snapshot_id,
            request_sha256=request_sha256(request),
            duration_ms=1,
            output={"summary": "done", "findings": [], "conflicts": [], "unknowns": []},
        )

    @staticmethod
    def assert_external_workspace(workspace: Path) -> None:
        if not workspace.is_dir():
            raise AssertionError("runner did not create an isolated provider workspace")


class SynthesisRunnerTests(TestCase):
    def test_default_is_a_side_effect_free_dry_run(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "repo"
            source.mkdir()
            plan = base / "plan.json"
            output = base / "results"
            _plan(plan)
            provider = RecordingProvider()

            summary = run_synthesis_plan(
                plan, source_root=source, output_dir=output, adapter=provider
            )

            self.assertFalse(summary["execute"])
            self.assertEqual(summary["sensitivity"], "private-source-derived")
            self.assertEqual(summary["status_counts"], {"planned": 2})
            self.assertEqual(provider.calls, 0)
            self.assertFalse(output.exists())

    def test_execution_is_atomic_parallel_and_resumes_only_exact_requests(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "repo"
            source.mkdir()
            plan = base / "plan.json"
            output = base / "results"
            _plan(plan)
            provider = RecordingProvider()

            first = run_synthesis_plan(
                plan,
                source_root=source,
                output_dir=output,
                adapter=provider,
                execute=True,
                concurrency=2,
            )
            second = run_synthesis_plan(
                plan,
                source_root=source,
                output_dir=output,
                adapter=provider,
                execute=True,
                concurrency=2,
            )
            changed = run_synthesis_plan(
                plan,
                source_root=source,
                output_dir=output,
                adapter=provider,
                execute=True,
                concurrency=2,
                model="different-model",
            )

            self.assertEqual(first["status_counts"], {"complete": 2})
            self.assertEqual(second["status_counts"], {"resumed": 2})
            self.assertEqual(changed["status_counts"], {"complete": 2})
            self.assertEqual(provider.calls, 4)
            self.assertFalse(list(output.rglob("*.tmp")))
            self.assertEqual(len(list((output / "jobs").glob("*.json"))), 4)
            receipt = json.loads(next((output / "jobs").glob("*.json")).read_text())
            self.assertEqual(receipt["sensitivity"], "private-source-derived")

    def test_output_inside_source_or_any_git_worktree_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "repo"
            source.mkdir()
            plan = base / "plan.json"
            _plan(plan, jobs=1)
            provider = RecordingProvider()

            with self.assertRaisesRegex(ValueError, "outside the analyzed repository"):
                run_synthesis_plan(
                    plan,
                    source_root=source,
                    output_dir=source / "results",
                    adapter=provider,
                )

            other_worktree = base / "other-worktree"
            other_worktree.mkdir()
            (other_worktree / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside Git worktrees"):
                run_synthesis_plan(
                    plan,
                    source_root=source,
                    output_dir=other_worktree / "results",
                    adapter=provider,
                )

    def test_local_provider_cannot_point_at_target_repository_code(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "repo"
            source.mkdir()
            target_script = source / "provider.py"
            target_script.write_text("raise SystemExit('must not run')\n", encoding="utf-8")
            plan = base / "plan.json"
            _plan(plan, jobs=1)
            adapter = LocalCommandProvider([sys.executable, str(target_script)])

            with self.assertRaisesRegex(ValueError, "cannot execute a path"):
                run_synthesis_plan(
                    plan,
                    source_root=source,
                    output_dir=base / "results",
                    adapter=adapter,
                    execute=True,
                )

    def test_invalid_bounds_and_duplicate_jobs_fail_before_execution(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "repo"
            source.mkdir()
            plan = base / "plan.json"
            _plan(plan)
            document = json.loads(plan.read_text(encoding="utf-8"))
            document["jobs"][1]["job_id"] = document["jobs"][0]["job_id"]
            plan.write_text(json.dumps(document), encoding="utf-8")
            provider = RecordingProvider()

            with self.assertRaisesRegex(ValueError, "unique"):
                run_synthesis_plan(
                    plan,
                    source_root=source,
                    output_dir=base / "results",
                    adapter=provider,
                    concurrency=2,
                )
            self.assertEqual(provider.calls, 0)

            with self.assertRaisesRegex(ValueError, "Concurrency"):
                run_synthesis_plan(
                    plan,
                    source_root=source,
                    output_dir=base / "results",
                    adapter=provider,
                    concurrency=17,
                )

    def test_declared_bound_cannot_hide_an_oversized_context_packet(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "repo"
            source.mkdir()
            plan = base / "plan.json"
            _plan(plan, jobs=1)
            document = json.loads(plan.read_text(encoding="utf-8"))
            document["jobs"][0]["context_pack"]["untrusted_padding"] = "x" * 1_000_001
            plan.write_text(json.dumps(document), encoding="utf-8")
            provider = RecordingProvider()

            with self.assertRaisesRegex(ValueError, "1 MB packet limit"):
                run_synthesis_plan(
                    plan,
                    source_root=source,
                    output_dir=base / "results",
                    adapter=provider,
                )
            self.assertEqual(provider.calls, 0)
