# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Reusing what a file already said, and proving it changed nothing.

The claim a cache makes is not that it is faster. It is that the run it
produced is the run that would have happened anyway, and speed is a
consequence. So the test is equality against a cold run, not a stopwatch: a
faster engine that answers differently is a different engine, which is the
same bar `benchmarks/scaling/run_corpus.py` holds worker counts to.

The safety half is the second group. A cached outcome is keyed on every input
the reader was given, and the one that matters is the set of module names in
the snapshot, because that is what decides whether a call lands in this
repository or outside it. A cache keyed on the file's bytes alone would hit
after a module was removed and serve a classification that is no longer true,
which is the failure `benchmarks/scaling/run_reader_locality.py` found and
this holds the door on.
"""

from __future__ import annotations

import sqlite3
import zlib
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton import reuse
from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers import python_ast
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.models import AnalysisResult
from open_skeleton.reuse import ReadCache
from open_skeleton.scanner import scan_repository

SERVICE = """\
import json
from pathlib import Path

from service.helper import tidy

RETRY_LIMIT = 3


def load(path: Path) -> dict:
    try:
        return tidy(json.loads(path.read_text()))
    except ValueError:
        raise RuntimeError("not JSON")
"""

HELPER = """\
def tidy(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if value is not None}
"""

STORE = """\
class Store:
    def latest(self) -> int:
        return 1
"""

# Calls a method on an instance of a class imported from another module of
# this repository. The reader records where that call lands, and answers
# `this repository` only because `service/store.py` is in the snapshot. Take
# it away and the same bytes here mean `dependency` instead, which is the
# whole reason a cache cannot be keyed on this file alone.
OTHER = """\
from service.helper import tidy
from service.store import Store


def run(rows: list[dict]) -> list[dict]:
    store = Store()
    return [tidy(row) for row in rows][: store.latest()]
"""


def _repository(root: Path) -> None:
    (root / "service").mkdir()
    (root / "service" / "__init__.py").write_text("", encoding="utf-8")
    (root / "service" / "loader.py").write_text(SERVICE, encoding="utf-8")
    (root / "service" / "helper.py").write_text(HELPER, encoding="utf-8")
    (root / "service" / "batch.py").write_text(OTHER, encoding="utf-8")
    (root / "service" / "store.py").write_text(STORE, encoding="utf-8")
    (root / "package.json").write_text('{"name":"fixture"}\n', encoding="utf-8")


def _call_origins(result: AnalysisResult) -> dict[str, str]:
    """Where each recorded call lands, by the file and call that made it."""

    found: dict[str, str] = {}
    for symbol in result.symbols:
        for name, info in ((symbol.metadata or {}).get("external_calls") or {}).items():
            found[f"{symbol.path}:{name}"] = str(info.get("origin"))
    return found


def _comparable(result: AnalysisResult) -> dict[str, Any]:
    """Everything a run produced except the clock readings in it."""

    def strip(record: Any) -> dict[str, Any]:
        values: dict[str, Any] = record.to_dict()
        for clock in ("created_at", "verified_at"):
            values.pop(clock, None)
        return values

    return {
        "snapshot_id": result.snapshot_id,
        "symbols": [strip(item) for item in result.symbols],
        "edges": [strip(item) for item in result.edges],
        "evidence": [strip(item) for item in result.evidence],
        "claims": [strip(item) for item in result.claims],
        "coverage": [item.to_dict() for item in result.coverage],
    }


def _python_files(root: Path) -> int:
    return len([item for item in scan_repository(root).files if item.language == "Python"])


class IncrementalAnalysisTests(TestCase):
    def test_a_reused_run_is_the_run_that_would_have_happened(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _repository(root)

            cache = ReadCache()
            analyze_snapshot(scan_repository(root), cache=cache)

            edited = root / "service" / "loader.py"
            edited.write_text(SERVICE.replace("RETRY_LIMIT = 3", "RETRY_LIMIT = 9"), "utf-8")

            snapshot = scan_repository(root)
            cold = analyze_snapshot(snapshot)
            warm = analyze_snapshot(snapshot, cache=cache)

        self.assertEqual(_comparable(warm), _comparable(cold))

    def test_only_the_edited_file_is_read_again(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _repository(root)
            total = _python_files(root)

            cache = ReadCache()
            analyze_snapshot(scan_repository(root), cache=cache)
            self.assertEqual((cache.hits, cache.misses), (0, total))

            edited = root / "service" / "loader.py"
            edited.write_text(SERVICE.replace("RETRY_LIMIT = 3", "RETRY_LIMIT = 9"), "utf-8")

            cache.hits = cache.misses = 0
            analyze_snapshot(scan_repository(root), cache=cache)

        self.assertEqual(
            (cache.hits, cache.misses),
            (total - 1, 1),
            "a one-line edit did not leave every other file's reading behind",
        )

    def test_a_reused_record_carries_the_clock_of_the_run_reusing_it(self) -> None:
        # Without this a receipt would keep the timestamp of the run that
        # first read it, and an incremental run would differ from a cold one
        # in a field nothing else would catch.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _repository(root)

            cache = ReadCache()
            first = analyze_snapshot(scan_repository(root), cache=cache)
            second = analyze_snapshot(scan_repository(root), cache=cache)

        self.assertNotEqual(first.created_at, second.created_at, "the clock did not move")
        reused = [item for item in second.evidence if item.path.startswith("service/")]
        self.assertTrue(reused, "fixture produced no reusable receipts")
        self.assertEqual(
            sorted({item.created_at for item in reused}),
            [second.created_at],
            "a reused receipt kept the timestamp of the run that first read it",
        )

    def test_nothing_is_reused_when_no_cache_is_supplied(self) -> None:
        # The default has to stay what it was: a library caller that asked for
        # nothing gets a pure function of the snapshot.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _repository(root)
            cache = ReadCache()
            snapshot = scan_repository(root)
            analyze_snapshot(snapshot, cache=cache)
            before = cache.hits
            analyze_snapshot(snapshot)

        self.assertEqual(cache.hits, before, "a cache nobody passed was consulted anyway")


class CacheSafetyTests(TestCase):
    """The conditions under which a cached outcome stops being an answer."""

    def test_removing_a_module_does_not_serve_the_old_classification(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _repository(root)

            cache = ReadCache()
            analyze_snapshot(scan_repository(root), cache=cache)

            # `batch.py` calls a method on a class imported from
            # `service/store.py`. Take that module away and the call is no
            # longer landing in this repository, although `batch.py` itself is
            # untouched.
            (root / "service" / "store.py").unlink()

            snapshot = scan_repository(root)
            cold = analyze_snapshot(snapshot)
            warm = analyze_snapshot(snapshot, cache=cache)

        # Stated first, so a fixture that stopped reproducing the
        # reclassification fails here rather than passing the comparison below
        # for having nothing to disagree about.
        self.assertEqual(
            _call_origins(cold).get("service/batch.py:store.latest"),
            "dependency",
            "the fixture no longer reclassifies a call when its module is removed; "
            "this test is guarding nothing",
        )
        self.assertEqual(
            _comparable(warm),
            _comparable(cold),
            "a cached outcome survived a change to the set of modules it was read against",
        )

    def test_adding_a_module_does_not_serve_the_old_classification(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _repository(root)

            cache = ReadCache()
            analyze_snapshot(scan_repository(root), cache=cache)

            (root / "service" / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")

            snapshot = scan_repository(root)
            cold = analyze_snapshot(snapshot)
            warm = analyze_snapshot(snapshot, cache=cache)

        self.assertEqual(_comparable(warm), _comparable(cold))

    def test_a_cache_from_another_reader_version_is_not_consulted(self) -> None:
        # The reader's version is in the key, so an upgraded reader cannot be
        # handed an answer the previous one gave.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _repository(root)
            cache = ReadCache()
            analyze_snapshot(scan_repository(root), cache=cache)
            versions = {key[0] for key in cache.entries}

        self.assertEqual(len(versions), 1, "entries were filed under more than one reader")
        self.assertTrue(
            next(iter(versions)).startswith("python-ast/"),
            "a cached entry is not filed under the reader that produced it",
        )


STATEFUL_PYTHON = """\
class Machine:
    def start(self) -> None:
        self.status = "idle"

    def run(self) -> None:
        if self.status == "idle":
            self.status = "running"
        else:
            self.status = "blocked"
"""

STATEFUL_TYPESCRIPT = """\
export class Machine {
  status = "idle";
  run() {
    if (this.status === "idle") {
      this.status = "running";
    } else {
      this.status = "blocked";
    }
  }
}
"""


def _tuple_paths(value: Any, trail: str = "") -> list[str]:
    """Every place a tuple sits inside a value, named by where it sits."""

    found: list[str] = []
    if isinstance(value, tuple):
        found.append(trail or "<root>")
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_tuple_paths(item, f"{trail}.{key}" if trail else str(key)))
    elif isinstance(value, list | tuple):
        for item in value:
            found.extend(_tuple_paths(item, f"{trail}[]"))
    return found


class RecordFidelityTests(TestCase):
    """A record has to survive being written down and read back.

    Every store this engine has puts records through JSON -- `metadata_json`
    in the ledger, the exports, and any cache that outlives a process -- and
    JSON has one sequence type. A tuple inside a record's free-form metadata
    is therefore a distinction only the in-memory record can see: it is
    invisible in everything persisted, and it makes a record rebuilt from any
    store compare unequal to the one a cold run built.

    `spec/diagrams.py` already coerces these values back with `tuple(item)`,
    which is the same fact noticed from the other end. This fails by naming
    the field, so a reader that introduces one is told where.
    """

    def test_no_record_holds_a_tuple_where_a_store_would_return_a_list(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app").mkdir()
            (root / "app" / "machine.py").write_text(STATEFUL_PYTHON, encoding="utf-8")
            (root / "app" / "machine.ts").write_text(STATEFUL_TYPESCRIPT, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))

        stateful = [item for item in result.symbols if "state_fields" in (item.metadata or {})]
        self.assertTrue(
            stateful,
            "the fixture recorded no state fields; this test is guarding nothing",
        )
        self.assertEqual(
            sorted({item.analyzer.split("/")[0] for item in stateful}),
            ["python-ast", "typescript-lexical"],
            "both readers that build state fields have to be covered",
        )

        offenders = sorted(
            f"{item.analyzer} {item.path} {where}"
            for item in result.symbols
            for where in _tuple_paths(item.metadata or {})
        )
        self.assertEqual(offenders[:5], [], "a record holds a tuple a store cannot return")


class StoredCacheTests(TestCase):
    """A cache that outlives its process, held to the same bar as one that does not.

    Every invocation of the command line is its own process, so without a
    store on disk the case this engine argues matters most -- a gate between
    an agent's turns -- is the one case that reuses nothing. What is stored
    has to come back equal, not merely similar: a rebuilt outcome that
    resembles the original produces a run that resembles a cold one.
    """

    def _store(self, root: Path, where: Path) -> ReadCache:
        cache = ReadCache()
        analyze_snapshot(scan_repository(root), cache=cache)
        reuse.save(where, cache, python_ast.outcome_to_json)
        return cache

    def test_a_stored_cache_produces_the_run_a_cold_read_would(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            _repository(root)
            store = Path(temporary) / "read-cache.sqlite3"
            self._store(root, store)

            loaded = reuse.load(store, python_ast.outcome_from_json)
            self.assertTrue(loaded.entries, "nothing was read back from the store")

            snapshot = scan_repository(root)
            cold = analyze_snapshot(snapshot)
            warm = analyze_snapshot(snapshot, cache=loaded)

        self.assertEqual(_comparable(warm), _comparable(cold))
        self.assertTrue(loaded.hits, "the stored entries were never used")

    def test_a_store_written_by_another_version_is_not_read(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            _repository(root)
            store = Path(temporary) / "read-cache.sqlite3"
            self._store(root, store)

            with closing(sqlite3.connect(store)) as connection:
                connection.execute(
                    "UPDATE metadata SET value = 'something-else' WHERE key = 'store_version'"
                )
                connection.commit()

            loaded = reuse.load(store, python_ast.outcome_from_json)

        self.assertEqual(loaded.entries, {}, "a store from another version was read anyway")

    def test_an_unreadable_store_is_a_slow_run_and_not_a_failed_one(self) -> None:
        with TemporaryDirectory() as temporary:
            store = Path(temporary) / "read-cache.sqlite3"
            store.write_bytes(b"this is not a database")
            loaded = reuse.load(store, python_ast.outcome_from_json)
            self.assertEqual(loaded.entries, {})

            missing = Path(temporary) / "absent.sqlite3"
            self.assertEqual(reuse.load(missing, python_ast.outcome_from_json).entries, {})

    def test_an_entry_that_will_not_decode_is_dropped_rather_than_guessed_at(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            _repository(root)
            store = Path(temporary) / "read-cache.sqlite3"
            self._store(root, store)

            with closing(sqlite3.connect(store)) as connection:
                total = connection.execute("SELECT COUNT(*) FROM read_cache").fetchone()[0]
                keys = [
                    str(row[0])
                    for row in connection.execute("SELECT key_sha256 FROM read_cache LIMIT 2")
                ]
                # Two ways an entry can be unusable, and both have to be
                # dropped rather than half-read: bytes that are not an
                # outcome, and bytes that are not even compressed.
                connection.execute(
                    "UPDATE read_cache SET outcome_deflated = ? WHERE key_sha256 = ?",
                    (zlib.compress(b'{"failure": null}', 1), keys[0]),
                )
                connection.execute(
                    "UPDATE read_cache SET outcome_deflated = ? WHERE key_sha256 = ?",
                    (b"not compressed at all", keys[1]),
                )
                connection.commit()

            loaded = reuse.load(store, python_ast.outcome_from_json)

        self.assertEqual(
            len(loaded.entries),
            total - 2,
            "a half-written entry was rebuilt from what happened to be there",
        )

    def test_only_what_is_missing_is_written(self) -> None:
        # Rewriting every entry costs about as much as encoding the repository
        # again, and almost all of it would be identical.
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            _repository(root)
            store = Path(temporary) / "read-cache.sqlite3"
            cache = self._store(root, store)
            again = reuse.save(store, cache, python_ast.outcome_to_json)

        self.assertEqual(again, 0, "entries the store already held were written again")

    def test_a_file_the_snapshot_no_longer_holds_is_forgotten(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            _repository(root)
            store = Path(temporary) / "read-cache.sqlite3"
            self._store(root, store)

            kept = {item.path for item in scan_repository(root).files}
            self.assertEqual(reuse.forget_absent(store, kept), 0)

            without = kept - {"service/batch.py"}
            self.assertEqual(reuse.forget_absent(store, without), 1)
            remaining = reuse.load(store, python_ast.outcome_from_json)

        self.assertNotIn(
            "service/batch.py",
            {key[1] for key in remaining.entries},
            "a file the snapshot no longer holds kept its entry",
        )


class LedgerWriteTests(TestCase):
    """Writing only what the ledger does not already hold.

    Re-analysing a repository mints the same identifier for every fact read
    from a file nobody touched, so writing those rows again says nothing new.
    On mypy that is 206,757 of 213,778 rows in what is now the largest stage
    of a run.

    Skipping by identifier alone would be wrong, and the first test here is
    why. Two fields move while an identifier stays put: a symbol's `metadata`,
    which records where each call lands, and an edge's `target_symbol_id`,
    which is resolved against every other file. A row digest covers both.
    """

    def _saved(self, root: Path, database: Path) -> EvidenceLedger:
        ledger = EvidenceLedger(database)
        snapshot = scan_repository(root)
        ledger.save_snapshot(snapshot)
        ledger.save_analysis(analyze_snapshot(snapshot))
        return ledger

    def _stored_metadata(self, database: Path, path: str) -> list[str]:
        with closing(sqlite3.connect(database)) as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    "SELECT metadata_json FROM symbols WHERE path = ? AND kind = 'module'",
                    (path,),
                )
            ]

    def test_a_row_whose_meaning_changed_is_written_although_its_name_did_not(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            _repository(root)
            database = Path(temporary) / "evidence.sqlite3"
            ledger = self._saved(root, database)
            before = self._stored_metadata(database, "service/batch.py")

            # `batch.py` calls a method on a class from `service/store.py`.
            # Its own bytes do not change, so its symbol keeps its identifier,
            # and what that symbol says about the call does change.
            (root / "service" / "store.py").unlink()
            snapshot = scan_repository(root)
            ledger.save_snapshot(snapshot)
            ledger.save_analysis(analyze_snapshot(snapshot))
            after = self._stored_metadata(database, "service/batch.py")

        self.assertTrue(before and after, "the fixture stored no module symbol to speak for")
        self.assertIn("this repository", before[0])
        self.assertNotIn(
            "this repository",
            after[0],
            "a symbol kept a classification the snapshot no longer supports; "
            "the write was skipped on an identifier that had not changed",
        )

    def test_an_incremental_ledger_says_what_a_cold_one_says(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            _repository(root)

            incremental = Path(temporary) / "incremental.sqlite3"
            ledger = self._saved(root, incremental)

            edited = root / "service" / "loader.py"
            edited.write_text(SERVICE.replace("RETRY_LIMIT = 3", "RETRY_LIMIT = 9"), "utf-8")

            snapshot = scan_repository(root)
            result = analyze_snapshot(snapshot)
            ledger.save_snapshot(snapshot)
            ledger.save_analysis(result)

            cold = Path(temporary) / "cold.sqlite3"
            fresh = EvidenceLedger(cold)
            fresh.save_snapshot(snapshot)
            fresh.save_analysis(result)

            rows = {}
            for name, database in (("incremental", incremental), ("cold", cold)):
                with closing(sqlite3.connect(database)) as connection:
                    connection.row_factory = sqlite3.Row
                    rows[name] = {
                        str(row["evidence_id"]): dict(row)
                        for row in connection.execute("SELECT * FROM evidence")
                    }

        shared = set(rows["incremental"]) & set(rows["cold"])
        self.assertTrue(shared, "the two ledgers share no receipts to compare")
        self.assertEqual(set(rows["cold"]) - shared, set(), "the cold ledger holds more")
        # `snapshot_id` and `created_at` are left as the run that first saw a
        # fact recorded them, which is the whole reason a row can be skipped.
        # Everything a receipt says has to match.
        differing = [
            key
            for key in sorted(shared)
            if {
                k: v
                for k, v in rows["incremental"][key].items()
                if k not in {"snapshot_id", "created_at"}
            }
            != {
                k: v for k, v in rows["cold"][key].items() if k not in {"snapshot_id", "created_at"}
            }
        ]
        self.assertEqual(
            differing[:5], [], "an incremental ledger says something a cold one does not"
        )

    def test_a_second_save_of_the_same_analysis_writes_nothing(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            _repository(root)
            database = Path(temporary) / "evidence.sqlite3"
            ledger = self._saved(root, database)
            first = ledger.rows_written
            self.assertTrue(first, "the first save wrote nothing at all")

            snapshot = scan_repository(root)
            ledger.save_analysis(analyze_snapshot(snapshot))

        self.assertEqual(ledger.rows_written, 0, "rows the ledger already held were written again")

    def test_only_the_edited_file_s_facts_are_written_again(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            _repository(root)
            database = Path(temporary) / "evidence.sqlite3"
            ledger = self._saved(root, database)
            everything = ledger.rows_written

            edited = root / "service" / "loader.py"
            edited.write_text(SERVICE.replace("RETRY_LIMIT = 3", "RETRY_LIMIT = 9"), "utf-8")
            snapshot = scan_repository(root)
            ledger.save_snapshot(snapshot)
            ledger.save_analysis(analyze_snapshot(snapshot))

        self.assertLess(
            ledger.rows_written,
            everything // 2,
            "a one-line edit rewrote most of the ledger",
        )
