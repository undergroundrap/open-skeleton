# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""What a per-file fact is named by, and what that name has to follow.

Two properties, and neither is safe without the other.

A fact's name must follow the bytes it was read from, so that a file nobody
touched keeps the same facts under a new snapshot. That is what makes
incremental analysis possible at all: identifiers that embed the snapshot id
give every fact in the repository a new name after a one-line edit, and
nothing computed for one snapshot can be matched to anything in the next.

A fact's name must also *change* when those bytes change. This is the half
that keeps the first one honest. The snapshot id used to supply both
properties by accident -- it is a digest of every file's content hash, so it
moved whenever anything moved. Take it out of an identifier without putting
the file's own content hash in, and two different versions of a line would
mint the same receipt id: the ledger would serve the old excerpt as evidence
for the new file and nothing would report a conflict, because as far as every
downstream check could tell, the receipt still verifies.

So the tests below are a pair. The first would pass on an engine that names
every receipt after its line number alone; the second is what forbids it.

Claims are deliberately excluded. A claim id hashes the snapshot, the category
and the text but not the path, because two files stating the same thing are
meant to merge into one claim with both receipts. Keying a claim by content
would produce two claims that `_merge_duplicate_claims` can no longer fold,
which is a different fact about the repository than the one being reported.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import fields as dataclass_fields
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository

# Records named after one file's contents. A census receipt is not among them:
# its path is `.` and its excerpt is the file inventory, so it is a fact about
# the snapshot and is expected to move whenever any file does.
PER_FILE = ("evidence", "symbols", "edges")

# What a census receipt puts in its `path`: not a file, but the snapshot. Its
# excerpt is the file inventory, so it is a fact about the whole repository and
# is expected to be renamed whenever the inventory changes.
SNAPSHOT_PATH = "."

# Readers the fixture has to exercise for this test to mean anything. A test
# that silently stopped covering a language would keep passing while that
# reader drifted, which is the failure this repository has hit before: an
# absence that reads as a clean bill of health.
REQUIRED_ANALYZERS = ("python-ast/", "typescript-lexical/", "rust-lexical/", "java-lexical/")

PYTHON = """\
import json
from pathlib import Path

RETRY_LIMIT = 3


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except ValueError:
        raise RuntimeError("not JSON")
"""

TYPESCRIPT = """\
import { useState } from "react";
export const LIMIT = 5;
export function Panel() {
  const [value, setValue] = useState(0);
  fetch("/api/panel").then(() => setValue(LIMIT));
  return value;
}
"""

RUST = """\
pub const RETRIES: u32 = 4;
pub struct Config { pub name: String }
impl Config {
    pub fn load(&self) -> Result<u32, String> {
        self.name.parse().map_err(|_| "bad".to_string())
    }
}
"""

JAVA = """\
package app;

public class Cart {
    private int items = 0;

    public int total() throws IllegalStateException {
        return items * 2;
    }
}
"""


def _repository(root: Path) -> None:
    (root / "service").mkdir()
    (root / "web").mkdir()
    (root / "crates").mkdir()
    (root / "java").mkdir()
    (root / "service" / "__init__.py").write_text("", encoding="utf-8")
    (root / "service" / "loader.py").write_text(PYTHON, encoding="utf-8")
    (root / "web" / "panel.ts").write_text(TYPESCRIPT, encoding="utf-8")
    (root / "crates" / "config.rs").write_text(RUST, encoding="utf-8")
    (root / "java" / "Cart.java").write_text(JAVA, encoding="utf-8")
    (root / "package.json").write_text('{"name":"fixture"}\n', encoding="utf-8")


def _facts(root: Path) -> dict[str, dict[str, str]]:
    """Every per-file record, as identifier mapped to the path it came from."""

    result = analyze_snapshot(scan_repository(root))
    found: dict[str, dict[str, str]] = {}
    for name in PER_FILE:
        found[name] = {}
        for record in getattr(result, name):
            identifier = getattr(record, f"{name[:-1] if name != 'evidence' else 'evidence'}_id")
            path = getattr(record, "path", None) or getattr(record, "source_path", "")
            found[name][identifier] = str(path)
    found["analyzers"] = {str(getattr(record, "analyzer", "")): "" for record in result.evidence}
    return found


class FactIdentityTests(TestCase):
    def test_the_fixture_exercises_every_reader_this_test_speaks_for(self) -> None:
        # Without this the two tests below could pass by covering nothing.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _repository(root)
            seen = set(_facts(root)["analyzers"])
        for prefix in REQUIRED_ANALYZERS:
            self.assertTrue(
                any(name.startswith(prefix) for name in seen),
                f"fixture produced no {prefix} evidence; this test no longer covers it",
            )

    def test_an_untouched_file_keeps_its_facts_under_a_new_snapshot(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _repository(root)
            before = _facts(root)

            # A new file nobody else refers to. The snapshot id changes because
            # it digests the whole inventory; no existing file's bytes do.
            (root / "service" / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
            after = _facts(root)

        for name in PER_FILE:
            kept = {
                identifier
                for identifier, path in before[name].items()
                if path not in {"service/extra.py", SNAPSHOT_PATH}
            }
            missing = sorted(kept - set(after[name]))
            self.assertEqual(
                missing[:5],
                [],
                f"{len(missing)} {name} identifier(s) changed although no file they "
                f"name was edited; a fact is still being named after its snapshot",
            )

        # The exclusion above has to earn its place. A census receipt is a fact
        # about the file inventory, so adding a file must rename it; if one
        # stopped doing that, the line above would be quietly excusing a
        # receipt that outlived what it counts.
        censuses = {
            identifier for identifier, path in before["evidence"].items() if path == SNAPSHOT_PATH
        }
        self.assertTrue(censuses, "fixture produced no census receipts to speak for")
        self.assertEqual(
            sorted(censuses & set(after["evidence"]))[:5],
            [],
            "a census receipt survived a change to the file inventory it counts",
        )

    def test_editing_a_file_renames_the_facts_read_from_it(self) -> None:
        # The safety half. Without it, dropping the snapshot id from an
        # identifier would let a receipt outlive the bytes it quotes.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _repository(root)
            before = _facts(root)

            edited = "service/loader.py"
            (root / edited).write_text(
                PYTHON.replace("RETRY_LIMIT = 3", "RETRY_LIMIT = 9").replace(
                    'raise RuntimeError("not JSON")', 'raise RuntimeError("bad payload")'
                ),
                encoding="utf-8",
            )
            after = _facts(root)

        for name in PER_FILE:
            from_edited = {
                identifier for identifier, path in before[name].items() if path == edited
            }
            self.assertTrue(from_edited, f"fixture produced no {name} for {edited}")
            # Naming is file-granular: a fact is named after the whole file it
            # was read from, so every fact from an edited file is renamed, and
            # a receipt cannot outlive the bytes it quotes. Making an untouched
            # span survive an edit elsewhere in its file is a separate change
            # -- finer-grained staleness, item 5 of the LANDSCAPE roadmap --
            # and it is not what this asserts.
            survived = sorted(from_edited & set(after[name]))
            self.assertEqual(
                survived[:5],
                [],
                f"{len(survived)} {name} identifier(s) survived an edit to the file "
                f"they were read from; a receipt can now outlive the bytes it quotes",
            )


class LedgerBindingTests(TestCase):
    """Two snapshots of one repository, in one ledger, both still readable.

    Naming a fact after its content is what makes it shareable between
    snapshots, and sharing is the point. But a ledger that stores the binding
    on the fact itself cannot hold a shared fact twice: the second snapshot's
    save updates the row it finds and the first snapshot loses it.

    That is not a hypothetical. Before the binding moved, saving a second
    snapshot of this fixture took the first snapshot's evidence from 34 rows
    to 5 -- every receipt read from a file both snapshots contain was
    re-labelled, leaving the first snapshot's claims verified with nothing
    behind them. It is the same failure the `INSERT OR REPLACE` cascade caused
    and `docs/PERFORMANCE.md` records, reached a different way, and it is
    invisible to any check that reads only the newest snapshot.
    """

    def test_a_second_snapshot_does_not_take_the_first_one_s_evidence(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            _repository(root)
            ledger = EvidenceLedger(Path(temporary) / "evidence.sqlite3")

            first = scan_repository(root)
            ledger.save_snapshot(first)
            ledger.save_analysis(analyze_snapshot(first))
            before = len(ledger.list_evidence(first.snapshot_id))
            self.assertTrue(before, "fixture recorded no evidence to speak for")

            (root / "service" / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
            second = scan_repository(root)
            ledger.save_snapshot(second)
            ledger.save_analysis(analyze_snapshot(second))

            self.assertEqual(
                len(ledger.list_evidence(first.snapshot_id)),
                before,
                "saving a second snapshot removed evidence from the first",
            )
            self.assertTrue(
                ledger.list_evidence(second.snapshot_id),
                "the second snapshot recorded no evidence of its own",
            )

    def test_a_shared_receipt_is_stored_once_and_answers_for_both(self) -> None:
        # The saving the shared naming is for. A file both snapshots contain
        # contributes one row, not one row per snapshot.
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            _repository(root)
            ledger = EvidenceLedger(Path(temporary) / "evidence.sqlite3")

            first = scan_repository(root)
            ledger.save_snapshot(first)
            ledger.save_analysis(analyze_snapshot(first))

            (root / "service" / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
            second = scan_repository(root)
            ledger.save_snapshot(second)
            ledger.save_analysis(analyze_snapshot(second))

            shared = {item["evidence_id"] for item in ledger.list_evidence(first.snapshot_id)} & {
                item["evidence_id"] for item in ledger.list_evidence(second.snapshot_id)
            }
            self.assertTrue(
                shared,
                "no receipt was shared between two snapshots of the same files; "
                "facts are still being named after the snapshot that read them",
            )

    def test_a_ledger_written_before_content_binding_still_answers(self) -> None:
        """Rows with no content hash keep the meaning they were written with.

        `file_sha256` is nullable because it has to be: a ledger written by an
        earlier version has no such column, and the additive migration adds it
        empty rather than inventing a value. A null is therefore not missing
        data to be repaired but a row whose binding is still its `snapshot_id`,
        and the membership predicate has to read it that way or an upgrade
        would silently empty every snapshot recorded before it.
        """

        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            _repository(root)
            database = Path(temporary) / "evidence.sqlite3"
            ledger = EvidenceLedger(database)

            snapshot = scan_repository(root)
            ledger.save_snapshot(snapshot)
            ledger.save_analysis(analyze_snapshot(snapshot))
            expected = len(ledger.list_evidence(snapshot.snapshot_id))
            self.assertTrue(expected, "fixture recorded no evidence to speak for")

            # What an upgraded ledger looks like the moment before anything is
            # written into it again.
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE evidence SET file_sha256 = NULL")
                connection.execute("UPDATE symbols SET file_sha256 = NULL")
                connection.execute("UPDATE edges SET file_sha256 = NULL")
                connection.commit()

            self.assertEqual(
                len(ledger.list_evidence(snapshot.snapshot_id)),
                expected,
                "upgrading a ledger lost the evidence it already held",
            )
            self.assertEqual(
                ledger.count_rows(snapshot.snapshot_id, "evidence"),
                expected,
                "counting disagreed with listing on a migrated ledger",
            )


class DistinctIdentityTests(TestCase):
    """One identifier names one fact.

    An identifier is a digest of what a fact says, so two facts that say
    different things must not share one. When they do, the ledger's primary
    key silently keeps whichever was written last and the other is gone: the
    receipt still verifies, the claim still cites it, and the fact it used to
    name is not there.

    This is checked over this repository rather than a fixture because the
    defect it first caught needed real source to appear: `sql-schema` minted a
    symbol for a table without recording where the statement was, so two
    `CREATE TABLE` statements naming different tables in one file collided.
    A fixture written to test the reader would not have had two.
    """

    def test_no_two_records_share_an_identifier_and_disagree(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = analyze_snapshot(scan_repository(root))

        clashes: list[str] = []
        for name, column in (
            ("evidence", "evidence_id"),
            ("symbols", "symbol_id"),
            ("edges", "edge_id"),
        ):
            seen: dict[str, str] = {}
            for record in getattr(result, name):
                identifier = str(getattr(record, column))
                spelled = "|".join(
                    f"{field.name}={getattr(record, field.name)!s}"
                    for field in dataclass_fields(record)
                    if field.name != column
                )
                if seen.setdefault(identifier, spelled) != spelled:
                    clashes.append(
                        f"{name} {identifier[:12]} "
                        f"({getattr(record, 'analyzer', '?')}) names two different facts"
                    )
            self.assertTrue(seen, f"the repository produced no {name} to check")

        self.assertEqual(sorted(set(clashes))[:6], [], "an identifier names more than one fact")
