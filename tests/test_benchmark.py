# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.benchmark import run_benchmark


class BenchmarkTests(TestCase):
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
