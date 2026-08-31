# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from open_skeleton.benchmark import _claim_matches, _git_commit, _load_gold, run_benchmark
from open_skeleton.models import ClaimRecord


class BenchmarkTests(TestCase):
    def test_git_receipt_trusts_only_the_exact_selected_fixture_for_the_command(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "fixture"
            root.mkdir()
            expected = "a" * 40

            with patch(
                "open_skeleton.benchmark.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, expected + "\n", ""),
            ) as run:
                observed = _git_commit(root)

        self.assertEqual(observed, expected)
        command = run.call_args.args[0]
        self.assertIn(f"safe.directory={root.resolve().as_posix()}", command)

    def _claim(self, text: str, category: str, status: str) -> ClaimRecord:
        return ClaimRecord(
            claim_id="claim",
            snapshot_id="snapshot",
            claim=text,
            category=category,
            status=status,
            confidence=1.0,
            importance="high",
            produced_by="test",
            created_at="now",
            supporting_evidence=("evidence",),
            contradicting_evidence=("contradiction",) if status == "conflict" else (),
        )

    def test_scores_receipted_claims_and_writes_reproducible_artifacts(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            repository = workspace / "repo"
            repository.mkdir()
            (repository / "app.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/health')\n"
                "def health():\n    return {'ok': True}\n",
                encoding="utf-8",
            )
            gold = workspace / "gold.json"
            gold.write_text(
                json.dumps(
                    {
                        "schema_version": "open-skeleton.benchmark.v1",
                        "fixture": {"name": "test"},
                        "precision_scope_categories": ["http_route_inventory"],
                        "baseline": {"name": "Manual baseline"},
                        "claims": [
                            {
                                "id": "route-count",
                                "area": "api",
                                "statement": "One HTTP route exists.",
                                "expected_status": "verified",
                                "match": {
                                    "category": "http_route_inventory",
                                    "statuses": ["verified"],
                                    "all_patterns": ["declares 1 HTTP route"],
                                },
                                "evidence_paths_any": ["app\\.py"],
                                "baseline": {"outcome": "miss", "evidence": "none"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = run_benchmark(repository, gold, workspace / "output")

            self.assertEqual(result["open_skeleton"]["recall"], 1.0)
            self.assertEqual(result["open_skeleton"]["precision"], 1.0)
            self.assertEqual(result["open_skeleton"]["evidence_correctness"], 1.0)
            self.assertTrue((workspace / "output" / "benchmark.json").exists())
            self.assertTrue((workspace / "output" / "benchmark.md").exists())

    def test_rejects_unsupported_gold_schema(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            repository = workspace / "repo"
            repository.mkdir()
            gold = workspace / "gold.json"
            gold.write_text('{"schema_version":"wrong","claims":[]}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Unsupported benchmark schema"):
                run_benchmark(repository, gold, workspace / "output")

    def test_pinned_matchers_follow_generalized_claim_categories(self) -> None:
        # Both facts remained in the output while their category or wording
        # became more general. A stale matcher reported them as regressions
        # even though the current claims and receipts carried the exact value.
        gold = _load_gold(
            Path(__file__).parents[1] / "benchmarks" / "single-player-ai-mud" / "gold.json"
        )
        specifications = {item["id"]: item for item in gold["claims"]}

        mathematical = self._claim(
            (
                "Exponential bases 1.15 and 1.1 are both raised to `ascensions`, so "
                "their ratio is (1.15/1.1)^N and grows without bound. Source comments "
                "contradict those formulas."
            ),
            "mathematical_conflict",
            "conflict",
        )
        absorbed = self._claim(
            (
                "backend.main.summarize_chat catches around `ai_client.generate_content` "
                "and returns empty string on failure, so a caller cannot tell it failed."
            ),
            "absorbed_failure",
            "verified",
        )

        self.assertTrue(
            _claim_matches(mathematical, specifications["unbounded-relative-scaling-conflict"])
        )
        self.assertTrue(_claim_matches(absorbed, specifications["partial-ai-fallbacks"]))

    def test_precision_scope_contains_only_exhaustively_adjudicated_categories(self) -> None:
        # The gold set names two table-creation facts. The SQL reader now also
        # emits three correct schema-detail claims, so treating every unmatched
        # storage_schema claim as incorrect makes added truth lower precision.
        gold = _load_gold(
            Path(__file__).parents[1] / "benchmarks" / "single-player-ai-mud" / "gold.json"
        )
        self.assertNotIn("storage_schema", gold["precision_scope_categories"])
