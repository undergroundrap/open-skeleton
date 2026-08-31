# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "comparison"))

from run_agent_parity import (
    _build_plan,
    _reconcile,
    _request_for_batch,
    _verify_frozen_repository,
)
from run_parity_gate import _adjudication_template, _evaluate, _evaluate_one
from run_parity_inventory import _content_blocks, _require_private_output


class ParityCorpusTests(TestCase):
    def test_every_nonblank_line_is_accounted_for_across_markdown_shapes(self) -> None:
        document = """\
# Architecture

The service owns state across two physical lines
and therefore cannot share it between processes.

- One constraint

| Field | Meaning |
|---|---|
| `cache` | Work set |

```python
# not a heading
run()
```

```mermaid
flowchart LR
  A --> B
```
"""

        blocks, accounting = _content_blocks(document, prefix="baseline")

        self.assertEqual(accounting["accounted_nonblank_lines"], accounting["nonblank_lines"])
        self.assertFalse(accounting["unaccounted_lines"])
        self.assertEqual(len({item["id"] for item in blocks}), len(blocks))
        self.assertEqual(
            {item["kind"] for item in blocks},
            {"heading", "prose", "list_item", "table_row", "presentation_only", "code", "diagram"},
        )
        self.assertFalse(
            any(item["kind"] == "heading" and "not a heading" in item["text"] for item in blocks)
        )

    def test_unclosed_fence_is_retained_instead_of_silently_dropped(self) -> None:
        blocks, accounting = _content_blocks(
            "# Example\n\n```text\nimportant conclusion\n", prefix="baseline"
        )

        self.assertEqual(accounting["accounted_nonblank_lines"], 3)
        self.assertEqual(blocks[-1]["kind"], "code")
        self.assertIn("important conclusion", blocks[-1]["text"])

    def test_private_output_is_rejected_inside_any_git_worktree(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            repository = workspace / "repository"
            repository.mkdir()
            (repository / ".git").mkdir()

            with self.assertRaisesRegex(ValueError, "Git worktree"):
                _require_private_output(repository / "reports")

    def test_private_output_is_rejected_inside_an_explicit_protected_root(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            protected = workspace / "analyzed"
            protected.mkdir()

            with self.assertRaisesRegex(ValueError, "cannot be written inside"):
                _require_private_output(protected / "reports", protected)


def _corpus() -> dict[str, Any]:
    baseline_blocks = [
        {
            "id": "baseline-one",
            "kind": "prose",
            "heading": "State",
            "start_line": 1,
            "end_line": 1,
            "text": "State is process local.",
            "text_sha256": "a" * 64,
        },
        {
            "id": "baseline-two",
            "kind": "heading",
            "heading": "State",
            "start_line": 2,
            "end_line": 2,
            "text": "## State",
            "text_sha256": "b" * 64,
        },
    ]
    candidate_blocks = [
        {
            "id": "candidate-one",
            "kind": "prose",
            "heading": "Ownership",
            "start_line": 4,
            "end_line": 4,
            "text": "The registry is process-local.",
            "text_sha256": "c" * 64,
        }
    ]
    return {
        "schema": "open-skeleton.parity-corpus.v1",
        "scope": "full-document",
        "sensitivity": "private-source-derived",
        "baseline": {"id": "external", "path": "baseline.md", "sha256": "d" * 64},
        "candidate": {"path": "candidate.md", "sha256": "e" * 64},
        "repository": {"commit": "f" * 40},
        "baseline_blocks": baseline_blocks,
        "candidate_blocks": candidate_blocks,
    }


def _proposal(
    unit_id: str,
    *,
    status: str = "equivalent",
    materiality: str = "material",
    baseline_validity: str = "supported",
    candidate_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": unit_id,
        "status": status,
        "materiality": materiality,
        "baseline_validity": baseline_validity,
        "proposal": True,
        "rationale": "Independent comparison.",
        "evidence_ids": [f"artifact:{unit_id}"],
        "candidate_unit_ids": candidate_ids if candidate_ids is not None else ["candidate-one"],
        "caveats": [],
    }


def _proposals() -> dict[str, Any]:
    return {
        "schema": "open-skeleton.parity-review-proposals.v1",
        "sensitivity": "private-source-derived",
        "scope": "full-document",
        "decisions": [
            {
                "id": unit_id,
                "state": "agent_consensus_proposal",
                "proposals": {
                    "codex": _proposal(unit_id),
                    "claude": _proposal(unit_id),
                },
            }
            for unit_id in ("baseline-one", "baseline-two")
        ],
    }


class AgentParityPlanTests(TestCase):
    def test_frozen_repository_is_reverified_before_review(self) -> None:
        corpus = _corpus()
        with TemporaryDirectory() as temporary:
            repository = Path(temporary) / "fixture"
            repository.mkdir()
            corpus["repository"] = {"path": str(repository), "commit": "f" * 40}
            receipt = {"clean": True, "commit": "f" * 40}

            with patch("run_agent_parity._verify_repository", return_value=receipt) as verify:
                observed = _verify_frozen_repository(corpus, {"id": "external"})

        self.assertEqual(observed, receipt)
        verify.assert_called_once()

    def test_plan_is_deterministic_and_covers_every_block_once(self) -> None:
        corpus = _corpus()

        models = {"codex": "codex-model", "claude": "claude-model"}
        first = _build_plan(corpus, corpus_sha256="1" * 64, batch_size=1, models=models)
        second = _build_plan(corpus, corpus_sha256="1" * 64, batch_size=1, models=models)

        self.assertEqual(first, second)
        self.assertEqual(first["batch_count"], 2)
        self.assertEqual(first["review_request_count"], 4)
        self.assertEqual(first["reviewer_models"], models)
        planned = [unit for batch in first["batches"] for unit in batch["unit_ids"]]
        self.assertEqual(planned, ["baseline-one", "baseline-two"])

    def test_request_contains_complete_candidate_corpus_and_block_receipts(self) -> None:
        corpus = _corpus()
        plan = _build_plan(corpus, corpus_sha256="1" * 64, batch_size=1)

        request = _request_for_batch(corpus, plan["batches"][0], model=None, timeout_seconds=30)

        self.assertTrue(request.context_pack["protocol"]["candidate_corpus_complete"])
        self.assertEqual(
            [item["id"] for item in request.context_pack["candidate_units"]],
            ["candidate-one"],
        )
        self.assertEqual(
            request.context_pack["evidence"][0]["evidence_id"],
            "artifact:baseline-one",
        )

    def test_matching_independent_proposals_are_consensus_but_not_final_parity(self) -> None:
        corpus = _corpus()
        plan = _build_plan(corpus, corpus_sha256="1" * 64, batch_size=2)
        reviews = [_proposal("baseline-one"), _proposal("baseline-two")]
        results = [
            {
                "batch_id": "batch-0001",
                "reviewer": reviewer,
                "status": "complete",
                "output": {"reviews": reviews},
            }
            for reviewer in ("codex", "claude")
        ]

        report = _reconcile(corpus, plan, results)

        self.assertEqual(report["summary"]["states"], {"agent_consensus_proposal": 2})
        self.assertFalse(report["summary"]["parity_proven"])

    def test_disagreement_and_baseline_incorrect_never_auto_resolve(self) -> None:
        corpus = _corpus()
        plan = _build_plan(corpus, corpus_sha256="1" * 64, batch_size=2)
        codex = [_proposal("baseline-one"), _proposal("baseline-two")]
        claude = [
            _proposal("baseline-one", status="missing", candidate_ids=[]),
            _proposal(
                "baseline-two",
                status="baseline_incorrect",
                baseline_validity="incorrect",
                candidate_ids=[],
            ),
        ]
        # Make both reviewers agree on the denominator-changing second unit.
        codex[1] = claude[1]
        results = [
            {
                "batch_id": "batch-0001",
                "reviewer": "codex",
                "status": "complete",
                "output": {"reviews": codex},
            },
            {
                "batch_id": "batch-0001",
                "reviewer": "claude",
                "status": "complete",
                "output": {"reviews": claude},
            },
        ]

        report = _reconcile(corpus, plan, results)

        self.assertEqual(report["summary"]["states"]["disputed"], 1)
        self.assertEqual(report["summary"]["states"]["human_required"], 1)

    def test_duplicate_provider_result_is_excluded_instead_of_overwriting(self) -> None:
        corpus = _corpus()
        plan = _build_plan(corpus, corpus_sha256="1" * 64, batch_size=2)
        reviews = [_proposal("baseline-one"), _proposal("baseline-two")]
        codex = {
            "batch_id": "batch-0001",
            "reviewer": "codex",
            "status": "complete",
            "output": {"reviews": reviews},
        }
        claude = {
            "batch_id": "batch-0001",
            "reviewer": "claude",
            "status": "complete",
            "output": {"reviews": reviews},
        }

        report = _reconcile(corpus, plan, [codex, codex, claude])

        self.assertEqual(report["summary"]["provider_errors"], 1)
        self.assertEqual(report["summary"]["states"], {"incomplete": 2})


class ParityGateTests(TestCase):
    def test_template_prefills_consensus_but_requires_human_verification(self) -> None:
        template = _adjudication_template(
            _corpus(),
            _proposals(),
            corpus_sha256="1" * 64,
            proposals_sha256="2" * 64,
        )

        first = template["adjudications"][0]
        self.assertEqual(first["proposal_disposition"], "accept_consensus")
        self.assertEqual(first["status"], "equivalent")
        self.assertFalse(first["all_atoms_verified"])
        self.assertEqual(first["reviewer"], "")

    def test_every_human_verified_consensus_can_prove_fixture_parity(self) -> None:
        corpus = _corpus()
        proposals = _proposals()
        template = _adjudication_template(
            corpus,
            proposals,
            corpus_sha256="1" * 64,
            proposals_sha256="2" * 64,
        )
        for item in template["adjudications"]:
            item["all_atoms_verified"] = True
            item["rationale"] = "Checked every assertion against the cited candidate block."
            item["reviewer"] = "human-one"

        proof = _evaluate(
            corpus,
            proposals,
            template,
            corpus_sha256="1" * 64,
            proposals_sha256="2" * 64,
            repository_receipt={"clean": True, "commit": "f" * 40},
        )

        self.assertTrue(proof["summary"]["parity_proven"])
        self.assertEqual(proof["summary"]["human_verified_blocks"], 2)

    def test_partial_material_coverage_fails_even_after_human_review(self) -> None:
        corpus = _corpus()
        proposals = _proposals()
        template = _adjudication_template(
            corpus,
            proposals,
            corpus_sha256="1" * 64,
            proposals_sha256="2" * 64,
        )
        for item in template["adjudications"]:
            item["all_atoms_verified"] = True
            item["rationale"] = "Reviewed."
            item["reviewer"] = "human-one"
        template["adjudications"][0]["proposal_disposition"] = "override"
        template["adjudications"][0]["status"] = "partial"

        proof = _evaluate(
            corpus,
            proposals,
            template,
            corpus_sha256="1" * 64,
            proposals_sha256="2" * 64,
            repository_receipt={"clean": True, "commit": "f" * 40},
        )

        self.assertFalse(proof["summary"]["parity_proven"])
        self.assertIn("not equivalent", proof["results"][0]["reasons"][0])

    def test_baseline_invalid_removal_requires_two_humans_and_repository_evidence(self) -> None:
        corpus = _corpus()
        proposals = _proposals()
        template = _adjudication_template(
            corpus,
            proposals,
            corpus_sha256="1" * 64,
            proposals_sha256="2" * 64,
        )
        item = template["adjudications"][0]
        item.update(
            {
                "proposal_disposition": "override",
                "materiality": "material",
                "baseline_validity": "incorrect",
                "status": "baseline_incorrect",
                "candidate_unit_ids": [],
                "all_atoms_verified": True,
                "rationale": "Repository evidence refutes this baseline statement.",
                "reviewer": "human-one",
            }
        )

        failed = _evaluate(
            corpus,
            proposals,
            template,
            corpus_sha256="1" * 64,
            proposals_sha256="2" * 64,
            repository_receipt={"clean": True, "commit": "f" * 40},
        )
        self.assertFalse(failed["results"][0]["passed"])

        item["second_reviewer"] = "human-two"
        item["repository_evidence_ids"] = ["claim:repo-source"]
        passed, reasons = _evaluate_one(
            item,
            proposals["decisions"][0],
            {"candidate-one"},
        )
        self.assertTrue(passed, reasons)
