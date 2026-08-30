# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""The head-to-head benchmark must not confuse baseline identity or revision.

There are exactly two external technical-specification runs in scope. Their
private Markdown exports are named publicly only by hashes, and a comparison
must fail closed before it measures a different artifact, commit, or dirty
working tree.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "comparison"))

from run_comparison import (
    BASELINE_SCHEMA,
    _load_baseline_record,
    _render,
    _validate_repository_state,
    _verify_baseline_artifact,
)
from run_reasoning_inventory import (
    ADJUDICATION_SCHEMA,
    TextUnit,
    _anchor_recall,
    _anchors,
    _best_candidate,
    _load_adjudications,
    _markdown_units,
    _reasoning_score,
    _tokens,
)

INVENTORY = Path(__file__).resolve().parents[1] / "benchmarks" / "comparison" / "baselines.json"


def _record(content: bytes = b"fixed baseline") -> dict[str, Any]:
    return {
        "id": "fixture",
        "provider": "External platform",
        "label": "a test export",
        "artifact": {
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
        "repository": {
            "commit": "a" * 40,
            "revision_certainty": "timestamp-inferred",
            "revision_basis": "bounded test window",
        },
        "generation": {"total_time_ms": 1_000_000},
    }


class BaselineInventoryTests(TestCase):
    def test_inventory_names_exactly_the_two_external_runs(self) -> None:
        document = json.loads(INVENTORY.read_text(encoding="utf-8"))
        records = {record["id"]: record for record in document["baselines"]}

        self.assertEqual(document["schema_version"], BASELINE_SCHEMA)
        self.assertEqual(
            set(records),
            {
                "external-open-skeleton-2026-08-07",
                "external-single-player-ai-mud-2026-08-04",
            },
        )
        self.assertEqual(
            {record["provider"] for record in document["baselines"]},
            {"External platform"},
        )
        self.assertTrue(all(not record["redistributed"] for record in document["baselines"]))
        self.assertEqual(
            records["external-single-player-ai-mud-2026-08-04"]["artifact"]["sha256"],
            "e70f4315c0c19b669bbf8d9ed9bbf8ae46e10216caf07308d0f3afe6fdeca138",
        )
        self.assertEqual(
            records["external-open-skeleton-2026-08-07"]["artifact"]["sha256"],
            "0c5d75f04335fae018216390f7a26be6e0613d5cd9c755caa9375710b21242ea",
        )

    def test_duplicate_baseline_id_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "baselines.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": BASELINE_SCHEMA,
                        "baselines": [{"id": "same"}, {"id": "same"}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Duplicate baseline id"):
                _load_baseline_record(path, "same")


class BaselineReceiptTests(TestCase):
    def test_matching_private_export_is_accepted_by_hash_and_size(self) -> None:
        content = b"fixed baseline"
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "tech_spec.md"
            path.write_bytes(content)

            receipt = _verify_baseline_artifact(path, _record(content))

        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["bytes"], len(content))
        self.assertEqual(receipt["sha256"], hashlib.sha256(content).hexdigest())

    def test_different_export_is_rejected_even_when_the_filename_matches(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "tech_spec.md"
            path.write_bytes(b"different baseline")

            with self.assertRaisesRegex(ValueError, "does not match"):
                _verify_baseline_artifact(path, _record())

    def test_wrong_repository_revision_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Repository revision does not match"):
            _validate_repository_state("b" * 40, "", _record())

    def test_dirty_repository_is_rejected_even_at_the_right_revision(self) -> None:
        with self.assertRaisesRegex(ValueError, "tracked or untracked changes"):
            _validate_repository_state("a" * 40, "?? local.txt", _record())

    def test_clean_matching_revision_preserves_its_certainty_disclosure(self) -> None:
        receipt = _validate_repository_state("A" * 40, "", _record())

        self.assertTrue(receipt["clean"])
        self.assertEqual(receipt["commit"], "a" * 40)
        self.assertEqual(receipt["revision_certainty"], "timestamp-inferred")


class ComparisonReportTests(TestCase):
    def setUp(self) -> None:
        self.ours: dict[str, Any] = {
            "seconds": 5.0,
            "artifact_sha256": "1" * 64,
            "citation_integrity": 1.0,
            "diagrams": {},
        }
        self.theirs: dict[str, Any] = {
            "seconds": 1000.0,
            "artifact_sha256": "2" * 64,
            "diagrams": {},
        }
        self.repository_receipt: dict[str, Any] = {
            "commit": "a" * 40,
            "revision_certainty": "timestamp-inferred",
            "revision_basis": "bounded test window",
        }

    def test_report_names_external_baseline_and_marks_timing_historical(self) -> None:
        report = _render(self.ours, self.theirs, _record(), self.repository_receipt)

        self.assertIn("External platform baseline", report)
        self.assertIn("0.5000% of the baseline's recorded wall time", report)
        self.assertIn("baseline/candidate elapsed-time ratio 200.0x", report)
        self.assertIn("historical observation, not a same-machine rerun", report)
        self.assertIn("timestamp-inferred", report)

    def test_missing_baseline_timing_does_not_invent_a_speed_ratio(self) -> None:
        self.theirs["seconds"] = None

        report = _render(self.ours, self.theirs, _record(), self.repository_receipt)

        self.assertNotIn("elapsed-time ratio", report)


class ReasoningInventoryTests(TestCase):
    def test_anchor_retrieval_matches_a_path_suffix_and_member_prefix(self) -> None:
        expected = frozenset({"simulation.py", "vec_db._zone_cache.keys"})
        observed = frozenset({"backend/app/core/simulation.py", "vec_db._zone_cache"})

        self.assertEqual(_anchor_recall(expected, observed), 1.0)

    def test_markdown_parser_does_not_invent_reasoning_from_a_code_fence(self) -> None:
        document = (
            "# Architecture\n\n"
            "Because `src/app.py` owns state, a second process cannot share it.\n\n"
            "```python\n"
            "# Because fake.py therefore means risk without state\n"
            "```\n"
            "## Runtime\n\n"
            "Ordinary descriptive text.\n"
        )

        units = _markdown_units(document)

        self.assertEqual(len(units), 2)
        self.assertEqual(units[0].heading, "Architecture")
        self.assertNotIn("fake.py", " ".join(item.text for item in units))
        self.assertGreaterEqual(_reasoning_score(units[0].text)[0], 5)
        self.assertEqual(_reasoning_score(units[1].text)[0], 0)

    def test_retrieval_exposes_similarity_without_calling_it_equivalence(self) -> None:
        baseline = "Because `src/app.py` owns process state, a second process cannot share it."
        candidate = TextUnit(
            heading="State",
            line=8,
            text="`src/app.py` contains process-local state and has no shared cache.",
        )

        best, score, anchor_recall, _ = _best_candidate(
            _tokens(baseline),
            _anchors(baseline),
            [(candidate, _tokens(candidate.text), _anchors(candidate.text))],
        )

        self.assertEqual(best, candidate)
        self.assertGreater(score, 0.5)
        self.assertEqual(anchor_recall, 1.0)

    def test_adjudication_sidecar_rejects_an_automated_similarity_status(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "adjudications.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": ADJUDICATION_SCHEMA,
                        "adjudications": [{"id": "one", "status": "likely_match"}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Unsupported reasoning adjudication status"):
                _load_adjudications(path)
