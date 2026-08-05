# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.python_ast import PythonAstAnalyzer
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository

PYTHON_FIXTURE = """\
import os
import sqlite3

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"])
cache = {}
cache["boot"] = True
connection = sqlite3.connect("fixture.db")
connection.execute("CREATE TABLE IF NOT EXISTS widgets (data TEXT)")


@app.get("/health")
def health(limit: int = 1) -> dict[str, str | None]:
    return {"mode": os.getenv("APP_MODE")}


if __name__ == "__main__":
    print(health())
"""


class PythonAnalyzerTests(TestCase):
    def test_python_facts_have_receipts_and_calibrated_status(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(PYTHON_FIXTURE, encoding="utf-8")
            (root / "test_app.py").write_text(
                "def test_truth():\n    assert True\n",
                encoding="utf-8",
            )

            snapshot = scan_repository(root)
            result = analyze_snapshot(snapshot)
            claims = {item.claim: item for item in result.claims}

            self.assertEqual(result.coverage[0].coverage_ratio, 1.0)
            self.assertTrue(any(item.qualified_name.endswith(".health") for item in result.symbols))
            self.assertTrue(any(item.relationship == "imports" for item in result.edges))
            self.assertIn("GET /health is handled by app.health.", claims)
            self.assertIn("Python source declares 1 HTTP route handlers.", claims)
            self.assertIn("app.health reads environment setting APP_MODE.", claims)
            self.assertIn("app creates SQLite table widgets.", claims)
            self.assertIn(
                "app configures CORSMiddleware with a wildcard allow_origins value.",
                claims,
            )
            self.assertTrue(any(item.category == "auth_control_census" for item in result.claims))
            self.assertTrue(
                any(item.category == "http_framework_behavior" for item in result.claims)
            )

            state_claim = claims[
                "app.cache is a module-owned mutable container with observed mutation sites; "
                "its contents are process-local unless code outside this module synchronizes "
                "them to durable storage."
            ]
            self.assertEqual(state_claim.status, "inferred")
            self.assertTrue(state_claim.alternative_hypotheses)

            evidence_ids = {item.evidence_id for item in result.evidence}
            for claim in result.claims:
                self.assertTrue(set(claim.supporting_evidence).issubset(evidence_ids))
                if claim.status == "verified":
                    self.assertTrue(claim.supporting_evidence)

    def test_changed_file_after_snapshot_is_reported_as_coverage_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            source.write_text("value = 1\n", encoding="utf-8")
            snapshot = scan_repository(root)
            source.write_text("value = 2\n", encoding="utf-8")

            result = analyze_snapshot(snapshot)

            self.assertEqual(result.coverage[0].analyzed_files, 0)
            self.assertEqual(result.coverage[0].failed_files, 1)
            self.assertIn("content changed after snapshot", result.coverage[0].failures[0])

    def test_analysis_persists_and_is_searchable(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            (root / "app.py").write_text(PYTHON_FIXTURE, encoding="utf-8")
            snapshot = scan_repository(root)
            result = analyze_snapshot(snapshot)
            ledger = EvidenceLedger(workspace / "state" / "evidence.sqlite3")

            ledger.save_snapshot(snapshot)
            run_id = ledger.save_analysis(result)

            latest = ledger.latest_analysis(snapshot.snapshot_id)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest["run_id"], run_id)
            claims = ledger.list_claims(snapshot.snapshot_id, category="http_route")
            self.assertEqual(len(claims), 1)
            matches = ledger.search_claims(snapshot.snapshot_id, "SQLite")
            self.assertGreaterEqual(len(matches), 1)
            receipt = ledger.get_evidence(claims[0]["supporting_evidence"][0])
            self.assertIsNotNone(receipt)

    def test_unimported_sibling_is_only_an_orphan_candidate(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "app" / "core"
            package.mkdir(parents=True)
            (root / "main.py").write_text(
                "from app.core.used import value\nprint(value)\n", encoding="utf-8"
            )
            (package / "used.py").write_text("value = 1\n", encoding="utf-8")
            (package / "unused.py").write_text("value = 2\n", encoding="utf-8")

            result = analyze_snapshot(scan_repository(root))
            candidates = [item for item in result.claims if item.category == "orphan_candidate"]

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].status, "inferred")
            self.assertIn("not a deletion instruction", candidates[0].claim)

    def test_state_reconciliation_math_conflict_and_operator_harness(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "backend").mkdir()
            (root / "scripts").mkdir()
            (root / "backend" / "main.py").write_text(
                """\
PLAYER_GROWTH = 1.15
_runs = {}

def login(player):
    if player.active_run_id and player.active_run_id not in _runs:
        player.active_run_id = None

def scale(player):
    # The wall stays non-trivial forever, never trivial.
    difficulty = 1.10 ** player.ascension_count
    power = PLAYER_GROWTH ** player.ascension_count
    return power / difficulty
""",
                encoding="utf-8",
            )
            (root / "scripts" / "smoke.py").write_text(
                "import sys\nif __name__ == '__main__':\n    sys.exit(0)\n",
                encoding="utf-8",
            )

            result = analyze_snapshot(scan_repository(root))
            categories = {item.category: item for item in result.claims}

            self.assertIn("state_reconciliation", categories)
            self.assertIn("mathematical_conflict", categories)
            self.assertEqual(categories["mathematical_conflict"].status, "conflict")
            self.assertTrue(categories["mathematical_conflict"].contradicting_evidence)
            self.assertIn("process_termination", categories)
            self.assertEqual(categories["process_termination"].status, "verified")
            self.assertIn("operator_harness", categories)
            self.assertEqual(categories["operator_harness"].status, "inferred")

    def test_testing_census_preserves_nonstandard_harness_alternative(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")

            result = analyze_snapshot(scan_repository(root))
            census = next(item for item in result.claims if item.category == "testing_census")

            self.assertEqual(census.status, "verified")
            self.assertIn("may still exist", census.claim)
            self.assertTrue(census.alternative_hypotheses)

    def test_analysis_reports_progress_after_each_adapter(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            events: list[tuple[str, int, int]] = []

            result = analyze_snapshot(
                scan_repository(root), on_event=lambda *event: events.append(event)
            )

            # One event per registered adapter, asserted against the adapters
            # that actually reported coverage rather than a hardcoded count, so
            # adding an analyzer does not require editing this expectation.
            reporting_analyzers = {item.analyzer for item in result.coverage}
            self.assertEqual(len(events), len(reporting_analyzers))
            self.assertEqual({name for name, _, _ in events}, reporting_analyzers)
            self.assertTrue(events[0][0].startswith("python-ast/"))
            self.assertTrue(all(elapsed >= 0 for _, elapsed, _ in events))

    def test_ai_exception_fallback_preserves_none_and_empty_values(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                """\
async def describe(ai_client):
    try:
        return {"description": await ai_client.generate_content("prompt")}
    except Exception:
        return {"description": None, "summary": ""}
""",
                encoding="utf-8",
            )

            result = analyze_snapshot(scan_repository(root))
            claim = next(item for item in result.claims if item.category == "ai_failure_behavior")

            self.assertEqual(claim.status, "verified")
            self.assertIn("None", claim.claim)
            self.assertIn("empty string", claim.claim)


class ControlFlowExtractionTests(TestCase):
    SOURCE = """
from fastapi import FastAPI, HTTPException
app = FastAPI()

@app.post("/a/{pid}")
async def handler(pid: str):
    if not lookup(pid):
        raise HTTPException(status_code=404, detail="missing")
    def helper():
        if never_reached:
            return 1
    for item in items:
        if item.bad:
            continue
    return {"ok": True}
"""

    def _flow(self, source: str) -> list[dict[str, object]]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text(source, encoding="utf-8")
            result = PythonAstAnalyzer().analyze(scan_repository(root))
        for symbol in result.symbols:
            flow = symbol.metadata.get("control_flow")
            if flow:
                return list(flow)
        return []

    def test_guards_raises_and_returns_are_recorded_in_line_order(self) -> None:
        flow = self._flow(self.SOURCE)
        kinds = [item["kind"] for item in flow]
        self.assertEqual(kinds[0], "guard")
        self.assertEqual(kinds[1], "raise")
        self.assertEqual(flow[-1]["kind"], "return")
        self.assertEqual([item["line"] for item in flow], sorted(item["line"] for item in flow))

    def test_a_nested_function_body_is_not_entered(self) -> None:
        flow = self._flow(self.SOURCE)
        labels = [item["label"] for item in flow]
        self.assertNotIn("never_reached", labels)

    def test_http_status_is_extracted_from_a_literal(self) -> None:
        flow = self._flow(self.SOURCE)
        raises = [item for item in flow if item["kind"] == "raise"]
        self.assertEqual(raises[0]["label"], "HTTP 404")

    def test_a_positional_status_argument_is_also_read(self) -> None:
        flow = self._flow(
            "from fastapi import FastAPI, HTTPException\n"
            "app = FastAPI()\n"
            '@app.get("/x")\n'
            "def h():\n"
            "    if bad:\n"
            "        raise HTTPException(429)\n"
            "    return 1\n"
        )
        raises = [item for item in flow if item["kind"] == "raise"]
        self.assertEqual(raises[0]["label"], "HTTP 429")

    def test_non_route_functions_record_no_control_flow(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "m.py").write_text(
                "def plain():\n    if x:\n        return 1\n", encoding="utf-8"
            )
            result = PythonAstAnalyzer().analyze(scan_repository(root))
        self.assertTrue(all("control_flow" not in item.metadata for item in result.symbols))
