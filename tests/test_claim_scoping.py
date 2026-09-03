# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Claims are filed by the role of the files they rest on.

The engine's own audit found this defect four times in four readers, each
fixed where it was noticed: routes, then schemas, then durable storage, then a
suite's `fetch` reported as a product's outbound HTTP surface. The rule now
lives at the one point that sees every claim against every file's role, so
these tests hold the rule rather than any one analyzer's memory of it.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import _scope_claims_by_evidence_role, analyze_snapshot
from open_skeleton.audit import PRODUCTION_CATEGORIES
from open_skeleton.models import ClaimRecord, EvidenceRecord, utc_now
from open_skeleton.policy import (
    HARNESS_SCOPED_CATEGORIES,
    TEST_SCOPED_CATEGORIES,
    exercises_the_product,
    scoped_category,
)
from open_skeleton.scanner import scan_repository


class ScopedCategoryPolicyTests(TestCase):
    def test_product_source_keeps_its_category(self) -> None:
        self.assertEqual(scoped_category("storage_schema", "source"), "storage_schema")

    def test_a_suite_and_a_benchmark_are_named_apart(self) -> None:
        # A benchmark is not a test, and `test_storage` on
        # `benchmarks/differential/run_sql_differential.py` would be as false
        # as the product category it replaced.
        self.assertEqual(scoped_category("storage", "test"), "test_storage")
        self.assertEqual(scoped_category("storage", "harness"), "harness_storage")

    def test_the_irregular_name_stays_irregular_in_both(self) -> None:
        # `http_route` scopes to `route`, not `http_route`. The harness names
        # are derived from the test mapping so one cannot drift from the other.
        self.assertEqual(scoped_category("http_route", "test"), "test_route")
        self.assertEqual(scoped_category("http_route", "harness"), "harness_route")
        self.assertEqual(set(HARNESS_SCOPED_CATEGORIES), set(TEST_SCOPED_CATEGORIES))

    def test_a_category_that_does_not_change_meaning_is_left_alone(self) -> None:
        # The table lists categories whose meaning depends on the role, not a
        # rule that everything gains a prefix. A public API is a public API.
        self.assertEqual(scoped_category("public_api", "test"), "public_api")
        self.assertEqual(scoped_category("public_api", "harness"), "public_api")

    def test_roles_that_are_neither_change_nothing(self) -> None:
        for role in ("documentation", "manifest", "workflow", "configuration", ""):
            self.assertEqual(scoped_category("storage", role), "storage")


class ScopeClaimsByEvidenceRoleTests(TestCase):
    """The pass itself, on evidence whose roles are known exactly."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        root = Path(self._temporary.name)
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "benchmarks").mkdir()
        (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "tests" / "test_app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "benchmarks" / "run_bench.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.snapshot = scan_repository(root)
        self.created_at = utc_now()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _evidence(self, path: str) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=f"evidence:{path}",
            snapshot_id=self.snapshot.snapshot_id,
            path=path,
            start_line=1,
            end_line=1,
            symbol=None,
            evidence_kind="statement",
            excerpt_sha256=None,
            analyzer="test",
            created_at=self.created_at,
        )

    def _claim(
        self, *paths: str, category: str = "storage", importance: str = "high"
    ) -> ClaimRecord:
        return ClaimRecord(
            claim_id="claim:one",
            snapshot_id=self.snapshot.snapshot_id,
            claim="something is stored",
            category=category,
            status="verified",
            confidence=1.0,
            importance=importance,
            produced_by="test",
            created_at=self.created_at,
            supporting_evidence=tuple(f"evidence:{path}" for path in paths),
        )

    def _scope(self, claim: ClaimRecord, *paths: str) -> ClaimRecord:
        scoped = _scope_claims_by_evidence_role(
            self.snapshot,
            [claim],
            [self._evidence(path) for path in paths],
        )
        return scoped[0]

    def test_a_claim_on_product_source_is_untouched(self) -> None:
        claim = self._claim("src/app.py")
        result = self._scope(claim, "src/app.py")
        self.assertEqual(result.category, "storage")
        self.assertEqual(result.importance, "high")

    def test_a_claim_resting_only_on_the_suite_describes_the_suite(self) -> None:
        result = self._scope(self._claim("tests/test_app.py"), "tests/test_app.py")
        self.assertEqual(result.category, "test_storage")

    def test_a_claim_resting_only_on_a_benchmark_describes_the_benchmark(self) -> None:
        result = self._scope(self._claim("benchmarks/run_bench.py"), "benchmarks/run_bench.py")
        self.assertEqual(result.category, "harness_storage")

    def test_a_refiled_claim_loses_a_product_finding_s_prominence(self) -> None:
        result = self._scope(self._claim("benchmarks/run_bench.py"), "benchmarks/run_bench.py")
        self.assertEqual(result.importance, "medium")

    def test_one_real_site_keeps_the_claim_about_the_system(self) -> None:
        # Evidence spanning the product and a benchmark still states something
        # true of the product; the benchmark receipt does not dilute it.
        claim = self._claim("src/app.py", "benchmarks/run_bench.py")
        result = self._scope(claim, "src/app.py", "benchmarks/run_bench.py")
        self.assertEqual(result.category, "storage")

    def test_evidence_spanning_two_exercising_roles_is_left_alone(self) -> None:
        # Both roles are non-product, but naming it after either would assert
        # something the evidence does not say. Left as it was, and visible.
        claim = self._claim("tests/test_app.py", "benchmarks/run_bench.py")
        result = self._scope(claim, "tests/test_app.py", "benchmarks/run_bench.py")
        self.assertEqual(result.category, "storage")

    def test_an_unresolvable_receipt_cannot_demote_a_finding(self) -> None:
        # A gap in bookkeeping is not evidence that a claim is about a test.
        claim = self._claim("tests/test_app.py", "src/missing.py")
        result = self._scope(claim, "tests/test_app.py")
        self.assertEqual(result.category, "storage")

    def test_an_aggregate_over_many_files_is_read_by_its_receipts(self) -> None:
        # The Rust panic census is one claim naming no path, over receipts
        # from many files. In a repository whose only Rust is a differential
        # reference implementation, every receipt is a benchmark -- and the
        # claim was this engine's reported panic surface.
        (Path(self.snapshot.root) / "benchmarks" / "second.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        snapshot = scan_repository(Path(self.snapshot.root))
        paths = ["benchmarks/run_bench.py", "benchmarks/second.py"]
        claim = self._claim(*paths, category="panic_site")
        scoped = _scope_claims_by_evidence_role(
            snapshot,
            [claim],
            [self._evidence(path) for path in paths],
        )
        self.assertEqual(scoped[0].category, "harness_panic_site")


class AnalyzedRepositoryScopingTests(TestCase):
    """End to end: the rule reaches a reader that never learned it."""

    HANDLER = (
        "import json\n"
        "\n"
        "def load(path):\n"
        "    try:\n"
        "        return json.loads(path.read_text())\n"
        "    except {exception}:\n"
        "        return None\n"
    )

    def test_a_benchmark_s_error_handling_is_not_the_product_s(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "benchmarks").mkdir()
            (root / "src" / "app.py").write_text(
                self.HANDLER.format(exception="OSError"), encoding="utf-8"
            )
            (root / "benchmarks" / "run_bench.py").write_text(
                self.HANDLER.format(exception="ValueError"), encoding="utf-8"
            )
            result = analyze_snapshot(scan_repository(root))

            by_category: dict[str, set[str]] = {}
            evidence_paths = {item.evidence_id: item.path for item in result.evidence}
            for claim in result.claims:
                paths = {evidence_paths.get(item, "") for item in claim.supporting_evidence}
                by_category.setdefault(claim.category, set()).update(paths)

            product = by_category.get("caught_exception", set())
            self.assertTrue(product, "the product's own handler should still be reported")
            self.assertNotIn("benchmarks/run_bench.py", product)
            self.assertIn(
                "benchmarks/run_bench.py",
                by_category.get("harness_caught_exception", set()),
            )


class RelocatedSourceTests(TestCase):
    """The miniature of `benchmarks/robustness/run_role_differential.py`.

    That instrument relocates a real package to find readers that ignore
    role; this keeps a small version of the same question in the gate, so a
    reader added later fails here rather than in a sweep nobody ran.
    """

    SOURCE = (
        "import os\n"
        "import sqlite3\n"
        "\n"
        "ENDPOINT = 'https://api.example.com/v1/items'\n"
        "\n"
        "def load(path):\n"
        "    token = os.environ.get('SERVICE_TOKEN')\n"
        "    try:\n"
        "        connection = sqlite3.connect(path)\n"
        "        return connection.execute('SELECT 1').fetchone()\n"
        "    except sqlite3.Error:\n"
        "        return None\n"
    )

    def _claims_describing_the_product(self, location: str) -> tuple[int, set[str]]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / location
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.SOURCE, encoding="utf-8")
            snapshot = scan_repository(root)
            result = analyze_snapshot(snapshot)

        role_by_path = {item.path: item.role for item in snapshot.files}
        path_by_evidence = {item.evidence_id: item.path for item in result.evidence}
        describing: set[str] = set()
        for claim in result.claims:
            if claim.category not in PRODUCTION_CATEGORIES:
                continue
            paths = {path_by_evidence.get(item) for item in claim.supporting_evidence}
            paths.discard(None)
            if not paths:
                continue
            if all(exercises_the_product(role_by_path.get(path or "")) for path in paths):
                describing.add(claim.category)
        return len(result.claims), describing

    def test_the_same_file_as_product_source_is_read_as_the_product(self) -> None:
        # Guards the rest of this class against passing because nothing was
        # read at all. A comparison between two empty answers proves nothing,
        # and this suite has been fooled that way before.
        total, _ = self._claims_describing_the_product("src/service.py")
        self.assertGreater(total, 3)

    def test_nothing_relocated_under_a_benchmark_describes_the_product(self) -> None:
        total, describing = self._claims_describing_the_product("benchmarks/service.py")
        self.assertGreater(total, 3)
        self.assertEqual(describing, set())

    def test_nothing_relocated_under_a_suite_describes_the_product(self) -> None:
        total, describing = self._claims_describing_the_product("tests/test_service.py")
        self.assertGreater(total, 3)
        self.assertEqual(describing, set())
