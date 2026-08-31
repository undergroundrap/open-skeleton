# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.cli import main
from open_skeleton.synthesis_assembly import (
    SYNTHESIS_ASSEMBLY_SCHEMA,
    assemble_synthesis,
)
from open_skeleton.synthesis_plan import SYNTHESIS_PLAN_SCHEMA
from open_skeleton.synthesis_runner import SYNTHESIS_JOB_RESULT_SCHEMA


def _write_plan(path: Path, *, jobs: int = 2) -> dict[str, object]:
    document: dict[str, object] = {
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
                    "claims": [
                        {
                            "claim_id": f"claim-{index}",
                            "claim": f"Claim {index}",
                        }
                    ],
                    "obligation": {
                        "section_id": f"section-{index}",
                        "number": f"{index}.1",
                        "title": f"Section {index}",
                    },
                },
            }
            for index in range(1, jobs + 1)
        ],
    }
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return document


def _plan_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_receipt(
    path: Path,
    *,
    plan_hash: str,
    job_index: int,
    job_id: str | None = None,
    status: str = "complete",
    claim_id: str | None = None,
) -> None:
    selected_job = job_id or f"section-{job_index}"
    request_hash = f"{job_index:064x}"
    output = {
        "summary": f"Summary {job_index}",
        "findings": [
            {
                "claim_ids": [claim_id or f"claim-{job_index}"],
                "narrative": f"Narrative {job_index}",
                "caveats": [f"Caveat {job_index}"],
            }
        ],
        "conflicts": [f"Conflict {job_index}"],
        "unknowns": [f"Unknown {job_index}"],
    }
    document = {
        "schema": SYNTHESIS_JOB_RESULT_SCHEMA,
        "sensitivity": "private-source-derived",
        "plan_sha256": plan_hash,
        "job_id": selected_job,
        "provider": "local-command",
        "request_sha256": request_hash,
        "result": {
            "provider": "local-command",
            "status": status,
            "snapshot_id": "snapshot",
            "request_sha256": request_hash,
            "duration_ms": 1,
            "output": output if status == "complete" else None,
            "error": None if status == "complete" else "provider failed",
            "metadata": {},
        },
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def _fixture(base: Path, *, jobs: int = 2) -> tuple[Path, Path, Path, str]:
    source = base / "repo"
    source.mkdir()
    (source / "spec.md").write_text("deterministic sentinel\n", encoding="utf-8")
    plan = base / "synthesis-plan.json"
    _write_plan(plan, jobs=jobs)
    plan_hash = _plan_hash(plan)
    results = base / "results"
    receipt_dir = results / "jobs"
    receipt_dir.mkdir(parents=True)
    for index in range(1, jobs + 1):
        _write_receipt(
            receipt_dir / f"job-{index}.json",
            plan_hash=plan_hash,
            job_index=index,
        )
    return source, plan, results, plan_hash


class SynthesisAssemblyTests(TestCase):
    def test_complete_receipts_render_a_separate_source_grounded_document(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, plan, results, plan_hash = _fixture(base)
            output = base / "assembled" / "source-grounded-synthesis.md"

            summary = assemble_synthesis(
                plan,
                results_dir=results,
                source_root=source,
                output_path=output,
            )

            markdown = output.read_text(encoding="utf-8")
            self.assertEqual(summary["schema"], SYNTHESIS_ASSEMBLY_SCHEMA)
            self.assertFalse(summary["contacts_model"])
            self.assertEqual(summary["plan_sha256"], plan_hash)
            self.assertEqual(summary["job_count"], 2)
            self.assertIn("## 1.1 Section 1", markdown)
            for expected in (
                "Summary 1",
                "Narrative 1",
                "claim-1",
                "Caveat 1",
                "Conflict 1",
                "Unknown 1",
            ):
                self.assertIn(expected, markdown)
            self.assertEqual(
                (source / "spec.md").read_text(encoding="utf-8"),
                "deterministic sentinel\n",
            )
            self.assertFalse(list(output.parent.glob("*.tmp")))

    def test_cli_assembles_without_contacting_a_provider(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, plan, results, _ = _fixture(base, jobs=1)
            state = base / "state"
            output = base / "cli-output.md"
            stdout = StringIO()

            with redirect_stdout(stdout):
                result = main(
                    [
                        "assemble-synthesis",
                        str(source),
                        "--state-dir",
                        str(state),
                        "--plan",
                        str(plan),
                        "--results-dir",
                        str(results),
                        "--output",
                        str(output),
                        "--json",
                    ]
                )

            summary = json.loads(stdout.getvalue())
            self.assertEqual(result, 0)
            self.assertFalse(summary["contacts_model"])
            self.assertEqual(Path(summary["artifact"]), output.resolve())
            self.assertTrue(output.is_file())

    def test_exact_plan_hash_and_job_coverage_are_required(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, plan, results, _ = _fixture(base)
            output = base / "assembled.md"
            first = results / "jobs" / "job-1.json"

            receipt = json.loads(first.read_text(encoding="utf-8"))
            receipt["plan_sha256"] = "f" * 64
            first.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exact frozen plan"):
                assemble_synthesis(
                    plan,
                    results_dir=results,
                    source_root=source,
                    output_path=output,
                )
            self.assertFalse(output.exists())

        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, plan, results, _ = _fixture(base)
            (results / "jobs" / "job-2.json").unlink()
            with self.assertRaisesRegex(ValueError, "Missing synthesis receipts"):
                assemble_synthesis(
                    plan,
                    results_dir=results,
                    source_root=source,
                    output_path=base / "assembled.md",
                )

        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, plan, results, plan_hash = _fixture(base)
            _write_receipt(
                results / "jobs" / "duplicate.json",
                plan_hash=plan_hash,
                job_index=1,
            )
            with self.assertRaisesRegex(ValueError, "Duplicate synthesis receipt"):
                assemble_synthesis(
                    plan,
                    results_dir=results,
                    source_root=source,
                    output_path=base / "assembled.md",
                )

        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, plan, results, plan_hash = _fixture(base)
            _write_receipt(
                results / "jobs" / "unknown.json",
                plan_hash=plan_hash,
                job_index=3,
                job_id="unknown-job",
            )
            with self.assertRaisesRegex(ValueError, "unknown job"):
                assemble_synthesis(
                    plan,
                    results_dir=results,
                    source_root=source,
                    output_path=base / "assembled.md",
                )

    def test_incomplete_or_ungrounded_provider_results_are_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, plan, results, plan_hash = _fixture(base, jobs=1)
            receipt = results / "jobs" / "job-1.json"
            _write_receipt(
                receipt,
                plan_hash=plan_hash,
                job_index=1,
                status="error",
            )
            with self.assertRaisesRegex(ValueError, "not a completed provider result"):
                assemble_synthesis(
                    plan,
                    results_dir=results,
                    source_root=source,
                    output_path=base / "assembled.md",
                )

        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, plan, results, plan_hash = _fixture(base, jobs=1)
            _write_receipt(
                results / "jobs" / "job-1.json",
                plan_hash=plan_hash,
                job_index=1,
                claim_id="invented-claim",
            )
            with self.assertRaisesRegex(ValueError, "outside its frozen job"):
                assemble_synthesis(
                    plan,
                    results_dir=results,
                    source_root=source,
                    output_path=base / "assembled.md",
                )

    def test_output_cannot_touch_source_git_or_deterministic_spec(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, plan, results, _ = _fixture(base, jobs=1)
            cases: tuple[tuple[Path, str], ...] = (
                (source / "assembled.md", "outside the analyzed repository"),
                (base / "spec.md", "cannot overwrite deterministic spec.md"),
            )
            git_root = base / "other-worktree"
            git_root.mkdir()
            (git_root / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
            cases += ((git_root / "assembled.md", "outside Git worktrees"),)

            for output, message in cases:
                with self.subTest(output=output), self.assertRaisesRegex(ValueError, message):
                    assemble_synthesis(
                        plan,
                        results_dir=results,
                        source_root=source,
                        output_path=output,
                    )
            self.assertEqual(
                (source / "spec.md").read_text(encoding="utf-8"),
                "deterministic sentinel\n",
            )
