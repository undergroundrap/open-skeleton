# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analyzers.hum_semantic_index import HumSemanticIndexAnalyzer
from open_skeleton.scanner import scan_repository


class HumSemanticIndexTests(TestCase):
    def test_missing_native_index_reports_precise_non_execution_limitation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "demo.hum").write_text("task greet\n", encoding="utf-8")

            result = HumSemanticIndexAnalyzer().analyze(scan_repository(root))

            self.assertEqual(result.coverage[0].unsupported_files, 1)
            self.assertEqual(result.coverage[0].coverage_ratio, 0.0)
            self.assertIn("did not execute the target compiler", result.coverage[0].failures[0])

    def test_consumes_versioned_native_graph_without_executing_hum(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            (root / "demo.hum").write_text("task greet\n", encoding="utf-8")
            index = workspace / "graph.json"
            index.write_text(
                json.dumps(
                    {
                        "schema": "hum.semantic_graph.v0",
                        "summary": {
                            "files": 1,
                            "items": 1,
                            "tasks": 1,
                            "tests": 0,
                            "errors": 0,
                            "warnings": 0,
                        },
                        "files": [
                            {
                                "path": "demo.hum",
                                "module": "demo",
                                "symbols": [
                                    {
                                        "id": "task:demo.hum:1:1:greet",
                                        "kind": "task",
                                        "name": "greet",
                                        "span": {"line": 1, "column": 1},
                                        "children": [],
                                    }
                                ],
                            }
                        ],
                        "diagnostics": [],
                    }
                ),
                encoding="utf-8",
            )

            result = HumSemanticIndexAnalyzer(index).analyze(scan_repository(root))

            self.assertEqual(result.coverage[0].coverage_ratio, 1.0)
            self.assertTrue(any(item.qualified_name == "demo.greet" for item in result.symbols))
            self.assertTrue(any(item.category == "hum_native_summary" for item in result.claims))
            self.assertTrue(all(item.excerpt_sha256 for item in result.evidence))
