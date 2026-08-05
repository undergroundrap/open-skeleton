# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.exports import export_jsonl, export_markdown
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository
from tests.helpers import create_sample_repository


class LedgerAndExportTests(TestCase):
    def test_snapshot_round_trip_and_exports(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            state = workspace / "state"
            root.mkdir()
            create_sample_repository(root)

            snapshot = scan_repository(root)
            ledger_path = state / "evidence.sqlite3"
            ledger = EvidenceLedger(ledger_path)
            ledger.save_snapshot(snapshot)
            latest = ledger.latest_snapshot()

            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest["snapshot_id"], snapshot.snapshot_id)
            self.assertEqual(latest["file_count"], 5)
            self.assertEqual(
                ledger.grouped_counts(snapshot.snapshot_id, "language")[0][0], "Python"
            )

            jsonl_path = state / "inventory.jsonl"
            markdown_path = state / "inventory.md"
            export_jsonl(snapshot, jsonl_path)
            export_markdown(snapshot, markdown_path)

            records = [
                json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(records[0]["record_type"], "snapshot")
            self.assertEqual(sum(record["record_type"] == "file" for record in records), 5)
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("# Repository Inventory", markdown)
            self.assertIn("without executing target code", markdown)

            with closing(sqlite3.connect(ledger_path)) as connection:
                schema_version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
            self.assertEqual(schema_version, ("4",))

    def test_repeated_save_is_idempotent(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            snapshot = scan_repository(root)
            ledger = EvidenceLedger(workspace / "state" / "evidence.sqlite3")

            ledger.save_snapshot(snapshot)
            ledger.save_snapshot(snapshot)

            with closing(sqlite3.connect(ledger.path)) as connection:
                file_count = connection.execute("SELECT COUNT(*) FROM files").fetchone()
                event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()
            self.assertEqual(file_count, (5,))
            self.assertEqual(event_count, (len(snapshot.events),))

    def test_changed_evidence_projects_old_claims_as_stale(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            source = root / "app.py"
            source.write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
            ledger = EvidenceLedger(workspace / "state" / "evidence.sqlite3")
            previous = scan_repository(root)
            ledger.save_snapshot(previous)
            ledger.save_analysis(analyze_snapshot(previous))

            source.write_text(
                "from fastapi import FastAPI\napp = FastAPI(title='changed')\n",
                encoding="utf-8",
            )
            current = scan_repository(root)
            ledger.save_snapshot(current)
            ledger.save_analysis(analyze_snapshot(current))

            difference = ledger.diff_snapshots(previous.snapshot_id, current.snapshot_id)
            stale = ledger.project_stale_claims(previous.snapshot_id, current.snapshot_id)

            self.assertEqual(difference["changed"], ["app.py"])
            self.assertTrue(stale)
            self.assertTrue(ledger.stale_claims(current.snapshot_id))
            historical = ledger.list_claims(previous.snapshot_id)
            self.assertTrue(all(item["status"] != "stale" for item in historical))


class AdditiveMigrationTests(TestCase):
    """A ledger written by an earlier version must keep working."""

    def _schema_three_ledger(self, path: Path) -> None:
        """Recreate the pre-migration analysis_coverage shape."""

        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE analysis_runs (run_id TEXT PRIMARY KEY);
                CREATE TABLE analysis_coverage (
                    run_id TEXT NOT NULL,
                    analyzer TEXT NOT NULL,
                    language TEXT NOT NULL,
                    eligible_files INTEGER NOT NULL,
                    analyzed_files INTEGER NOT NULL,
                    failed_files INTEGER NOT NULL,
                    unsupported_files INTEGER NOT NULL,
                    failures_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, analyzer, language)
                );
                INSERT INTO metadata VALUES ('schema_version', '3');
                INSERT INTO analysis_runs VALUES ('run-1');
                INSERT INTO analysis_coverage
                    VALUES ('run-1', 'python-ast/v2', 'Python', 4, 4, 0, 0, '[]');
                """
            )
            connection.commit()

    def test_missing_column_is_added_without_dropping_rows(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.sqlite3"
            self._schema_three_ledger(path)

            EvidenceLedger(path).initialize()

            with closing(sqlite3.connect(path)) as connection:
                connection.row_factory = sqlite3.Row
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(analysis_coverage)")
                }
                rows = connection.execute("SELECT * FROM analysis_coverage").fetchall()
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()

            self.assertIn("claimed_files", columns)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["analyzed_files"], 4)
            self.assertEqual(version["value"], "4")

    def test_a_migrated_row_reports_unknown_yield_rather_than_zero(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.sqlite3"
            self._schema_three_ledger(path)
            EvidenceLedger(path).initialize()

            with closing(sqlite3.connect(path)) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute("SELECT claimed_files FROM analysis_coverage").fetchone()

            # Zero would assert the analyzer found nothing, which is not known.
            self.assertIsNone(row["claimed_files"])

    def test_migration_is_idempotent(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.sqlite3"
            self._schema_three_ledger(path)

            EvidenceLedger(path).initialize()
            EvidenceLedger(path).initialize()

            with closing(sqlite3.connect(path)) as connection:
                connection.row_factory = sqlite3.Row
                columns = [
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(analysis_coverage)")
                ]
            self.assertEqual(columns.count("claimed_files"), 1)
