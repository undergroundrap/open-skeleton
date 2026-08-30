# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.models import AnalysisResult, ClaimRecord, EvidenceRecord, utc_now
from open_skeleton.scanner import scan_repository
from open_skeleton.spec import build_spec, load_profile
from open_skeleton.synthesis_plan import SYNTHESIS_PLAN_SCHEMA, build_synthesis_plan
from tests.helpers import create_sample_repository


class SynthesisPlanTests(TestCase):
    def test_plan_covers_every_non_structural_obligation_without_running_a_model(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            snapshot = scan_repository(root)
            ledger = EvidenceLedger(workspace / "state" / "evidence.sqlite3")
            ledger.save_snapshot(snapshot)
            ledger.save_analysis(analyze_snapshot(snapshot))
            document = build_spec(ledger, load_profile())

            plan = build_synthesis_plan(document, ledger, max_chars=5_000)

            expected = [item for item in document.sections if item.verdict != "structural"]
            self.assertEqual(plan["schema"], SYNTHESIS_PLAN_SCHEMA)
            self.assertFalse(plan["contacts_model"])
            self.assertEqual(plan["job_count"], len(expected))
            self.assertTrue(all(item["parallel_safe"] for item in plan["jobs"]))
            self.assertEqual(
                {item["job_id"] for item in plan["jobs"]},
                {item.section_id for item in expected},
            )
            claimed_job = next(item for item in plan["jobs"] if item["requested_claim_count"])
            available = {claim["claim_id"] for claim in claimed_job["context_pack"]["claims"]}
            self.assertTrue(available)
            self.assertEqual(
                claimed_job["context_pack"]["obligation"]["section_id"],
                claimed_job["job_id"],
            )

    def test_context_pack_includes_supporting_and_contradicting_receipts(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            source = root / "decision.txt"
            source.write_text("enabled\ndisabled\n", encoding="utf-8")
            snapshot = scan_repository(root)
            ledger = EvidenceLedger(workspace / "state" / "evidence.sqlite3")
            ledger.save_snapshot(snapshot)
            now = utc_now()
            evidence = (
                EvidenceRecord(
                    evidence_id="support",
                    snapshot_id=snapshot.snapshot_id,
                    path="decision.txt",
                    start_line=1,
                    end_line=1,
                    symbol=None,
                    evidence_kind="test",
                    excerpt_sha256=hashlib.sha256(b"enabled\n").hexdigest(),
                    analyzer="test/v1",
                    created_at=now,
                ),
                EvidenceRecord(
                    evidence_id="contradiction",
                    snapshot_id=snapshot.snapshot_id,
                    path="decision.txt",
                    start_line=2,
                    end_line=2,
                    symbol=None,
                    evidence_kind="test",
                    excerpt_sha256=hashlib.sha256(b"disabled\n").hexdigest(),
                    analyzer="test/v1",
                    created_at=now,
                ),
            )
            claim = ClaimRecord(
                claim_id="conflicted-claim",
                snapshot_id=snapshot.snapshot_id,
                claim="The feature state conflicts across declarations.",
                category="test_conflict",
                status="conflict",
                confidence=1.0,
                importance="high",
                produced_by="test/v1",
                created_at=now,
                supporting_evidence=("support",),
                contradicting_evidence=("contradiction",),
            )
            ledger.save_analysis(
                AnalysisResult(
                    snapshot_id=snapshot.snapshot_id,
                    analyzer_version="test/v1",
                    created_at=now,
                    duration_ms=0,
                    symbols=(),
                    edges=(),
                    evidence=evidence,
                    claims=(claim,),
                    coverage=(),
                )
            )

            with patch.object(ledger, "list_claims", side_effect=AssertionError):
                pack = ledger.context_pack(snapshot.snapshot_id, "conflicts")

            self.assertEqual(
                {item["relationship"] for item in pack["evidence"]},
                {"supports", "contradicts"},
            )
            self.assertFalse(pack["truncated"])

    def test_exact_plan_selection_reports_unknown_claim_ids(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            snapshot = scan_repository(root)
            ledger = EvidenceLedger(workspace / "state" / "evidence.sqlite3")
            ledger.save_snapshot(snapshot)

            pack = ledger.context_pack_for_claims(
                snapshot.snapshot_id,
                ["not-in-this-snapshot"],
                query="adversarial missing claim",
            )

            self.assertEqual(pack["missing_claim_ids"], ["not-in-this-snapshot"])
            self.assertTrue(pack["truncated"])
