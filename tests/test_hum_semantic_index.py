# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analyzers.hum_semantic_index import HumSemanticIndexAnalyzer
from open_skeleton.models import AnalysisResult
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


class HumGraphFactTests(TestCase):
    """The compiler's own findings, stated with the line they point at.

    Ingesting only the shape of a program read 229 files of hum-lang and
    produced one claim: full coverage and no yield, a number green because
    the analyzer reached the code and silent about whether it understood any
    of it.

    The index already carried more. `hum graph` emits its own diagnostics
    with codes and spans, and the predicate places that make up a task's
    declared contract, so none of this is invented -- the compiler decided
    which facts were worth emitting and where they point.
    """

    SOURCE = "task divide\n  needs: b != 0\n  ensures: result * b == a\n  body\n"

    def _analyze(self, document: dict[str, object]) -> AnalysisResult:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            (root / "divide.hum").write_text(self.SOURCE, encoding="utf-8")
            index = workspace / "graph.json"
            index.write_text(json.dumps(document), encoding="utf-8")
            return HumSemanticIndexAnalyzer([index]).analyze(scan_repository(root))

    def _document(self, **extra: object) -> dict[str, object]:
        base: dict[str, object] = {
            "schema": "hum.semantic_graph.v0",
            "summary": {"files": 1, "items": 1, "tasks": 1, "tests": 0, "errors": 0},
            "files": [{"path": "divide.hum", "module": "divide", "symbols": []}],
        }
        base.update(extra)
        return base

    def test_a_declared_contract_becomes_a_claim_with_a_receipt(self) -> None:
        result = self._analyze(
            self._document(
                predicate_place_facts=[
                    {
                        "task": "divide",
                        "section": "needs",
                        "span": {"file": "divide.hum", "line": 2, "column": 3},
                    }
                ]
            )
        )
        contracts = [c for c in result.claims if c.category == "hum_contract"]
        self.assertEqual(len(contracts), 1)
        self.assertIn("`divide`", contracts[0].claim)
        self.assertIn("`needs` clause", contracts[0].claim)
        self.assertTrue(contracts[0].supporting_evidence)

    def test_a_contract_claim_does_not_assert_the_contract_holds(self) -> None:
        # A declared clause is a statement the code makes about itself. That
        # it is checked anywhere is a different fact this does not have.
        result = self._analyze(
            self._document(
                predicate_place_facts=[
                    {
                        "task": "divide",
                        "section": "ensures",
                        "span": {"file": "divide.hum", "line": 3},
                    }
                ]
            )
        )
        claim = next(c for c in result.claims if c.category == "hum_contract")
        self.assertIn("not a check that it holds", claim.claim)

    def test_diagnostics_are_grouped_by_code_rather_than_listed(self) -> None:
        # Four hundred rows of one rule is the shape that teaches a reader to
        # skip the section.
        result = self._analyze(
            self._document(
                diagnostics=[
                    {
                        "code": "H0107",
                        "title": "task missing needs section",
                        "severity": "warning",
                        "span": {"file": "divide.hum", "line": line},
                    }
                    for line in (1, 2, 3)
                ]
            )
        )
        found = [c for c in result.claims if c.category == "hum_diagnostic"]
        self.assertEqual(len(found), 1)
        self.assertIn("3 warning(s) of H0107", found[0].claim)
        self.assertEqual(len(found[0].supporting_evidence), 3)

    def test_a_fixture_diagnostic_is_not_reported_beside_a_real_one(self) -> None:
        # A compiler's negative fixtures are malformed on purpose so a rule
        # fires. Counted together with real sources this reported "24 error(s)
        # in the analyzed sources" for a language project whose actual
        # programs had none -- every error it had was a fixture proving a rule
        # works, which is the opposite of a defect.
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            (root / "fixtures").mkdir(parents=True)
            (root / "divide.hum").write_text(self.SOURCE, encoding="utf-8")
            (root / "fixtures" / "bad.hum").write_text(self.SOURCE, encoding="utf-8")
            document = self._document(
                files=[
                    {"path": "divide.hum", "module": "divide", "symbols": []},
                    {"path": "fixtures/bad.hum", "module": "bad", "symbols": []},
                ],
                diagnostics=[
                    {
                        "code": "H0612",
                        "title": "app start duplicated",
                        "severity": "error",
                        "span": {"file": path, "line": 1},
                    }
                    for path in ("divide.hum", "fixtures/bad.hum")
                ],
            )
            index = workspace / "graph.json"
            index.write_text(json.dumps(document), encoding="utf-8")
            result = HumSemanticIndexAnalyzer([index]).analyze(scan_repository(root))

        found = {
            ("fixture" if "test material, where" in c.claim else "real"): c
            for c in result.claims
            if c.category == "hum_diagnostic"
        }
        self.assertEqual(set(found), {"fixture", "real"})
        self.assertIn("outside the test material", found["real"].claim)
        self.assertEqual(found["real"].importance, "high")
        # A rule firing where it was meant to fire is evidence the rule works.
        self.assertEqual(found["fixture"].importance, "low")

    def test_an_error_outranks_a_warning(self) -> None:
        result = self._analyze(
            self._document(
                diagnostics=[
                    {
                        "code": "H0612",
                        "title": "app start duplicated",
                        "severity": "error",
                        "span": {"file": "divide.hum", "line": 1},
                    }
                ]
            )
        )
        claim = next(c for c in result.claims if c.category == "hum_diagnostic")
        self.assertEqual(claim.importance, "high")
        self.assertIn("1 error(s)", claim.claim)

    def test_a_span_outside_the_snapshot_is_not_cited(self) -> None:
        # An index may describe files the snapshot excluded. Citing one would
        # produce a receipt pointing at nothing.
        result = self._analyze(
            self._document(
                diagnostics=[
                    {
                        "code": "H0107",
                        "title": "t",
                        "severity": "warning",
                        "span": {"file": "elsewhere/other.hum", "line": 1},
                    }
                ]
            )
        )
        self.assertEqual([c for c in result.claims if c.category == "hum_diagnostic"], [])

    def test_an_index_without_either_section_still_analyzes(self) -> None:
        result = self._analyze(self._document())
        self.assertEqual(result.coverage[0].analyzed_files, 1)
