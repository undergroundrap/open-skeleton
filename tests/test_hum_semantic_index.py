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


class ShardedIndexTests(TestCase):
    """Whole-repo coverage may arrive as several indexes rather than one."""

    @staticmethod
    def _index(path: Path, *files: str) -> Path:
        path.write_text(
            json.dumps(
                {
                    "schema": "hum.semantic_graph.v0",
                    "summary": {
                        "files": len(files),
                        "items": len(files),
                        "tasks": 0,
                        "tests": 0,
                        "errors": 0,
                    },
                    "files": [
                        {
                            "id": f"file:{name}",
                            "path": name,
                            "module": name.removesuffix(".hum"),
                            "symbols": [{"name": "greet", "kind": "task", "span": {"line": 1}}],
                        }
                        for name in files
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def _workspace(self, temporary: str) -> Path:
        workspace = Path(temporary)
        root = workspace / "repo"
        root.mkdir()
        for name in ("alpha.hum", "beta.hum"):
            (root / name).write_text("task greet\n", encoding="utf-8")
        return workspace

    def test_two_indexes_together_cover_every_file(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary)
            first = self._index(workspace / "a.json", "alpha.hum")
            second = self._index(workspace / "b.json", "beta.hum")

            result = HumSemanticIndexAnalyzer([first, second]).analyze(
                scan_repository(workspace / "repo")
            )

            coverage = result.coverage[0]
            self.assertEqual(coverage.eligible_files, 2)
            self.assertEqual(coverage.analyzed_files, 2)
            self.assertEqual(coverage.coverage_ratio, 1.0)

    def test_each_index_keeps_its_own_hashed_receipt(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary)
            first = self._index(workspace / "a.json", "alpha.hum")
            second = self._index(workspace / "b.json", "beta.hum")

            result = HumSemanticIndexAnalyzer([first, second]).analyze(
                scan_repository(workspace / "repo")
            )

            receipts = [
                item for item in result.evidence if item.evidence_kind == "native_semantic_index"
            ]
            self.assertEqual(len(receipts), 2)
            self.assertEqual(len({item.excerpt_sha256 for item in receipts}), 2)

    def test_a_file_covered_twice_is_analyzed_once(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary)
            first = self._index(workspace / "a.json", "alpha.hum", "beta.hum")
            second = self._index(workspace / "b.json", "alpha.hum")

            result = HumSemanticIndexAnalyzer([first, second]).analyze(
                scan_repository(workspace / "repo")
            )

            modules = [item for item in result.symbols if item.kind == "module"]
            self.assertEqual(len(modules), 2)
            self.assertEqual(result.coverage[0].analyzed_files, 2)

    def test_one_unreadable_index_does_not_discard_the_others(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary)
            good = self._index(workspace / "a.json", "alpha.hum")
            broken = workspace / "b.json"
            broken.write_text("{not json", encoding="utf-8")

            result = HumSemanticIndexAnalyzer([good, broken]).analyze(
                scan_repository(workspace / "repo")
            )

            self.assertEqual(result.coverage[0].analyzed_files, 1)
            self.assertTrue(any("JSONDecodeError" in item for item in result.coverage[0].failures))

    def test_a_single_path_is_still_accepted(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary)
            only = self._index(workspace / "a.json", "alpha.hum")

            result = HumSemanticIndexAnalyzer(only).analyze(scan_repository(workspace / "repo"))

            self.assertEqual(result.coverage[0].analyzed_files, 1)

    def test_guidance_names_the_multi_path_invocation(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary)

            result = HumSemanticIndexAnalyzer().analyze(scan_repository(workspace / "repo"))

            guidance = result.coverage[0].failures[0]
            self.assertIn("accepts multiple paths", guidance)
            self.assertIn("Repeat --hum-index", guidance)
