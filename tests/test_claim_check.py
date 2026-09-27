# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.cli import main
from open_skeleton.ledger import EvidenceLedger, FileChange, staleness_reason
from open_skeleton.mcp_server import OpenSkeletonService
from open_skeleton.models import AnalysisResult, ClaimRecord, Snapshot
from open_skeleton.scanner import scan_repository

HOOK_PANEL = """\
import { useState } from "react";
export function Panel() {
  const [value, setValue] = useState(0);
  return value;
}
"""


def _repository(root: Path) -> None:
    (root / "web").mkdir()
    (root / "crates").mkdir()
    (root / "web" / "first.tsx").write_text(HOOK_PANEL, encoding="utf-8")
    (root / "web" / "second.tsx").write_text(HOOK_PANEL, encoding="utf-8")
    (root / "crates" / "lib.rs").write_text("pub fn answer() -> u32 { 42 }\n", encoding="utf-8")


def _inventory(root: Path) -> dict[str, tuple[str, str]]:
    return {item.path: (item.sha256, item.language) for item in scan_repository(root).files}


def _hook_claim(result: AnalysisResult, path: str) -> ClaimRecord:
    return next(
        claim
        for claim in result.claims
        if claim.category == "ui_state" and claim.claim.startswith(f"{path} ")
    )


def _unsafe_census(result: AnalysisResult) -> ClaimRecord:
    return next(claim for claim in result.claims if claim.category == "unsafe_surface")


class _Fixture:
    def __init__(self, workspace: Path) -> None:
        self.root = workspace / "repo"
        self.root.mkdir()
        _repository(self.root)
        self.snapshot: Snapshot = scan_repository(self.root)
        self.result = analyze_snapshot(self.snapshot)
        self.ledger = EvidenceLedger(workspace / "state" / "evidence.sqlite3")
        self.ledger.save_snapshot(self.snapshot)
        self.ledger.save_analysis(self.result)

    def check(self, *claim_ids: str) -> dict[str, Any]:
        return self.ledger.check_claims(self.snapshot.snapshot_id, claim_ids, _inventory(self.root))


def _verdicts(report: dict[str, Any]) -> list[str]:
    return [item["verdict"] for item in report["claims"]]


class ClaimCheckTests(TestCase):
    def test_an_unchanged_repository_leaves_every_claim_current(self) -> None:
        with TemporaryDirectory() as temporary:
            fixture = _Fixture(Path(temporary))
            first = _hook_claim(fixture.result, "web/first.tsx")
            census = _unsafe_census(fixture.result)
            report = fixture.check(first.claim_id, census.claim_id)
        self.assertEqual(_verdicts(report), ["current", "current"])
        self.assertEqual(report["files_changed"], 0)

    def test_an_edit_stales_the_claims_that_rest_on_it_and_no_others(self) -> None:
        with TemporaryDirectory() as temporary:
            fixture = _Fixture(Path(temporary))
            first = _hook_claim(fixture.result, "web/first.tsx")
            second = _hook_claim(fixture.result, "web/second.tsx")
            census = _unsafe_census(fixture.result)
            with (fixture.root / "web" / "first.tsx").open("a", encoding="utf-8") as handle:
                handle.write("// edited\n")
            report = fixture.check(first.claim_id, second.claim_id, census.claim_id)

        by_id = {item["claim_id"]: item for item in report["claims"]}
        self.assertEqual(by_id[first.claim_id]["verdict"], "stale")
        self.assertIn("changed dependency file:web/first.tsx", by_id[first.claim_id]["reasons"])
        self.assertEqual(
            {item["path"] for item in by_id[first.claim_id]["moved_receipts"]}, {"web/first.tsx"}
        )
        # The neighbouring file and a census over another language are untouched.
        self.assertEqual(by_id[second.claim_id]["verdict"], "current")
        self.assertEqual(by_id[census.claim_id]["verdict"], "current")

    def test_a_language_census_goes_stale_when_that_language_changes(self) -> None:
        # "No `unsafe` in the 1 analyzed Rust files" is a statement about every
        # Rust file. Stale projection ignored `language:` keys, so it could
        # never go stale however much Rust changed.
        with TemporaryDirectory() as temporary:
            fixture = _Fixture(Path(temporary))
            census = _unsafe_census(fixture.result)
            self.assertIn("language:rust", census.invalidation_keys)
            (fixture.root / "crates" / "extra.rs").write_text(
                "pub unsafe fn raw() {}\n", encoding="utf-8"
            )
            report = fixture.check(census.claim_id)
        [item] = report["claims"]
        self.assertEqual(item["verdict"], "stale")
        self.assertIn("a rust file changed", item["reasons"])

    def test_a_removed_file_is_named_as_removed(self) -> None:
        with TemporaryDirectory() as temporary:
            fixture = _Fixture(Path(temporary))
            second = _hook_claim(fixture.result, "web/second.tsx")
            (fixture.root / "web" / "second.tsx").unlink()
            report = fixture.check(second.claim_id)
        [item] = report["claims"]
        self.assertEqual(item["verdict"], "stale")
        self.assertEqual({receipt["file"] for receipt in item["moved_receipts"]}, {"removed"})
        self.assertEqual(report["files_removed"], 1)

    def test_an_unknown_claim_is_not_found_rather_than_current(self) -> None:
        with TemporaryDirectory() as temporary:
            fixture = _Fixture(Path(temporary))
            report = fixture.check("no-such-claim")
        self.assertEqual(_verdicts(report), ["not-found"])
        self.assertEqual(report["counts"]["not-found"], 1)

    def test_a_key_no_inventory_can_evaluate_is_named_not_assumed(self) -> None:
        with TemporaryDirectory() as temporary:
            fixture = _Fixture(Path(temporary))
            base = _hook_claim(fixture.result, "web/second.tsx")
            pinned = replace(
                base,
                claim_id=f"{base.claim_id}-git",
                invalidation_keys=("git:HEAD",),
            )
            fixture.ledger.save_analysis(
                replace(
                    fixture.result, created_at="2099-01-01T00:00:00.000+00:00", claims=(pinned,)
                )
            )
            report = fixture.check(pinned.claim_id)
        [item] = report["claims"]
        self.assertEqual(item["unevaluated_keys"], ["git:HEAD"])

    def test_checking_writes_nothing(self) -> None:
        with TemporaryDirectory() as temporary:
            fixture = _Fixture(Path(temporary))
            first = _hook_claim(fixture.result, "web/first.tsx")
            (fixture.root / "web" / "first.tsx").write_text("export {};\n", encoding="utf-8")

            def counts() -> list[int]:
                with closing(sqlite3.connect(fixture.ledger.path)) as connection:
                    return [
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                        for table in ("snapshots", "claims", "claim_validity", "analysis_runs")
                    ]

            before = counts()
            fixture.check(first.claim_id)
            self.assertEqual(counts(), before)

    def test_at_most_five_hundred_claims_per_check(self) -> None:
        with TemporaryDirectory() as temporary:
            fixture = _Fixture(Path(temporary))
            with self.assertRaises(ValueError):
                fixture.check(*(f"claim-{index}" for index in range(501)))


class StaleProjectionTests(TestCase):
    def test_the_stored_projection_uses_the_same_rules(self) -> None:
        with TemporaryDirectory() as temporary:
            fixture = _Fixture(Path(temporary))
            census = _unsafe_census(fixture.result)
            first = _hook_claim(fixture.result, "web/first.tsx")
            (fixture.root / "crates" / "lib.rs").write_text(
                "pub fn answer() -> u32 { 43 }\n", encoding="utf-8"
            )
            current = scan_repository(fixture.root)
            fixture.ledger.save_snapshot(current)
            stale = fixture.ledger.project_stale_claims(
                fixture.snapshot.snapshot_id, current.snapshot_id
            )
        stale_ids = {item["claim_id"] for item in stale}
        self.assertIn(census.claim_id, stale_ids)
        self.assertNotIn(first.claim_id, stale_ids)

    def test_rules(self) -> None:
        change = FileChange.between(
            {"a.py": ("1", "Python"), "b.rs": ("2", "Rust"), "c.ts": ("3", "TypeScript")},
            {"a.py": ("1", "Python"), "b.rs": ("9", "Rust"), "d.ts": ("4", "TypeScript")},
        )
        self.assertEqual(change.changed, {"b.rs"})
        self.assertEqual(change.added, {"d.ts"})
        self.assertEqual(change.removed, {"c.ts"})
        paths = {"pkg.Thing": "b.rs", "pkg.Other": "a.py"}
        stale = {
            key: staleness_reason(key, change, paths)
            for key in (
                "file:b.rs",
                "file:a.py",
                "language:rust",
                "language:python",
                "language:typescript",
                "snapshot:file-set",
                "python:import-graph",
                "python:exception-handling",
                "symbol:pkg.Thing",
                "symbol:pkg.Other",
                "symbol:pkg.Unknown",
            )
        }
        self.assertEqual(
            {key for key, reason in stale.items() if reason},
            {
                "file:b.rs",
                "language:rust",
                "language:typescript",
                "snapshot:file-set",
                "symbol:pkg.Thing",
            },
        )


class CheckSurfaceTests(TestCase):
    def test_the_command_exits_by_verdict(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            state = workspace / "state"
            root.mkdir()
            _repository(root)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    main(["analyze", str(root), "--state-dir", str(state), "--quiet"]), 0
                )
            ledger = EvidenceLedger(state / "evidence.sqlite3")
            snapshot_id = str(ledger.snapshots_for_root(root.resolve(), limit=1)[0]["snapshot_id"])
            claim_id = next(
                item["claim_id"]
                for item in ledger.list_claims(snapshot_id, category="ui_state")
                if str(item["claim"]).startswith("web/first.tsx ")
            )
            base = ["check", "--path", str(root), "--state-dir", str(state)]

            with redirect_stdout(StringIO()):
                self.assertEqual(main([*base, claim_id]), 0)
            (root / "web" / "first.tsx").write_text("export {};\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([*base, claim_id, "--json"]), 1)
            self.assertEqual(json.loads(output.getvalue())["counts"]["stale"], 1)
            with redirect_stdout(StringIO()):
                self.assertEqual(main([*base, "no-such-claim"]), 1)

            unanalyzed = workspace / "other"
            unanalyzed.mkdir()
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "check",
                            "--path",
                            str(unanalyzed),
                            "--state-dir",
                            str(workspace / "s2"),
                            claim_id,
                        ]
                    ),
                    2,
                )

    def test_the_mcp_service_answers_without_a_refresh(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            _repository(root)
            service = OpenSkeletonService(root, workspace / "state")
            service.refresh_analysis()
            claim_id = next(
                item["claim_id"]
                for item in service.list_claims(category="ui_state")
                if str(item["claim"]).startswith("web/second.tsx ")
            )
            before = service.project_status()
            (root / "web" / "second.tsx").write_text("export {};\n", encoding="utf-8")
            report = service.check_claims([claim_id])
            after = service.project_status()
        self.assertEqual(_verdicts(report), ["stale"])
        # Still the snapshot it was analysed at: checking is not refreshing.
        self.assertEqual(before, after)
