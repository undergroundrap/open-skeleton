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

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
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
