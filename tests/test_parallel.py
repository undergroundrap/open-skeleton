# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import os
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase, mock

from open_skeleton import parallel
from open_skeleton.analysis import analyze_snapshot
from open_skeleton.models import AnalysisResult, Snapshot
from open_skeleton.scanner import scan_repository

PYTHON_MODULE = """\
import json
import os
from fastapi import FastAPI

app = FastAPI()
RETRY_LIMIT = {index}
_cache = {{}}

@app.get("/items/{{item_id}}")
def read_item(item_id: int, verbose: bool = False) -> dict:
    try:
        payload = json.loads(os.environ.get("PAYLOAD_{index}", "{{}}"))
    except ValueError:
        raise RuntimeError("payload {index} is not JSON")
    _cache[item_id] = payload
    return {{"id": item_id, "attempts": RETRY_LIMIT * 2}}
"""

TYPESCRIPT_MODULE = """\
import {{ useState }} from "react";
export const LIMIT_{index} = {index};
export function Panel{index}() {{
  const [value, setValue] = useState(0);
  fetch("/api/panel/{index}").then(() => setValue(LIMIT_{index}));
  localStorage.setItem("panel-{index}", String(value));
  return value;
}}
"""

RUST_MODULE = """\
pub const RETRIES_{index}: u32 = {index};
pub struct Config{index} {{ pub name: String }}
impl Config{index} {{
    pub fn load(&self) -> Result<u32, String> {{
        let value: u32 = self.name.parse().map_err(|_| "bad".to_string())?;
        if value == 0 {{ panic!("zero"); }}
        Ok(value.unwrap_or_default())
    }}
}}
#[test]
fn loads_{index}() {{ assert_eq!(1, 1); }}
"""


def _repository(root: Path) -> None:
    (root / "service").mkdir()
    (root / "web").mkdir()
    (root / "crates").mkdir()
    (root / "tests").mkdir()
    (root / "service" / "__init__.py").write_text("", encoding="utf-8")
    for index in range(12):
        (root / "service" / f"module_{index:02d}.py").write_text(
            PYTHON_MODULE.format(index=index), encoding="utf-8"
        )
        (root / "web" / f"panel_{index:02d}.tsx").write_text(
            TYPESCRIPT_MODULE.format(index=index), encoding="utf-8"
        )
        (root / "crates" / f"config_{index:02d}.rs").write_text(
            RUST_MODULE.format(index=index), encoding="utf-8"
        )
    # A file the Python reader must report as a failure, from whichever worker
    # happens to read it, in the place a serial run reports it.
    (root / "service" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (root / "tests" / "test_service.py").write_text(
        "from service.module_00 import read_item\n\ndef test_read():\n    assert read_item(1)\n",
        encoding="utf-8",
    )
    (root / "schema.sql").write_text(
        "CREATE TABLE players (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("# Fixture\n\nRun `python -m service`.\n", encoding="utf-8")


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


class ParallelAnalysisTests(TestCase):
    def test_workers_change_nothing_but_the_time_taken(self) -> None:
        # The threshold is lowered so a small fixture takes the parallel path;
        # everything else is the path a large repository takes. Two and three
        # workers split the files differently, and both must agree with the
        # serial run record for record, failures and their order included.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _repository(root)
            snapshot = scan_repository(root)
            serial = _comparable(analyze_snapshot(snapshot, jobs=1))
            with mock.patch.object(parallel, "MIN_PARALLEL_BYTES", 0):
                for jobs in (2, 3):
                    with self.subTest(jobs=jobs):
                        self.assertEqual(_comparable(analyze_snapshot(snapshot, jobs=jobs)), serial)

        languages = {item["language"]: item for item in serial["coverage"]}
        # The fixture exercises all three readers that share their files out,
        # and a failure; otherwise agreement would prove little.
        self.assertEqual(languages["Python"]["failed_files"], 1)
        self.assertGreaterEqual(languages["Python"]["analyzed_files"], 13)
        self.assertEqual(languages["JavaScript/TypeScript"]["analyzed_files"], 12)
        self.assertEqual(languages["Rust"]["analyzed_files"], 12)
        self.assertGreater(len(serial["claims"]), 20)

    def test_workers_are_separate_processes(self) -> None:
        # The fallback makes a broken pool invisible in the result, which is
        # its purpose. This is the check that the pool is not always broken.
        snapshot = GatherTests().snapshot()
        with parallel.start_pool(snapshot, 2) as executor:
            futures = parallel.submit_chunks(executor, _process_id, [[1], [2]], None)
            identities = [future.result() for future in futures]
        self.assertTrue(all(pid != os.getpid() for [pid] in identities))

    def test_progress_is_reported_once_per_analyzer_in_either_mode(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _repository(root)
            snapshot = scan_repository(root)
            serial: list[str] = []
            analyze_snapshot(snapshot, jobs=1, on_event=lambda name, _ms, _n: serial.append(name))
            pooled: list[str] = []
            with mock.patch.object(parallel, "MIN_PARALLEL_BYTES", 0):
                analyze_snapshot(
                    snapshot, jobs=2, on_event=lambda name, _ms, _n: pooled.append(name)
                )
        self.assertEqual(sorted(pooled), sorted(serial))
        self.assertEqual(len(set(serial)), len(serial))

    def test_a_small_repository_never_starts_a_pool(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
            snapshot = scan_repository(root)
            with mock.patch.object(
                parallel, "start_pool", side_effect=AssertionError("pool started")
            ):
                result = analyze_snapshot(snapshot, jobs=8)
        self.assertTrue(result.symbols)

    def test_a_pool_that_cannot_start_falls_back_to_a_serial_run(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _repository(root)
            snapshot = scan_repository(root)
            serial = _comparable(analyze_snapshot(snapshot, jobs=1))
            with (
                mock.patch.object(parallel, "MIN_PARALLEL_BYTES", 0),
                mock.patch(
                    "open_skeleton.analysis.start_pool", side_effect=OSError("no processes")
                ),
            ):
                fallback = _comparable(analyze_snapshot(snapshot, jobs=4))
        self.assertEqual(fallback, serial)


def _process_id(_snapshot: Snapshot, _chunk: list[int], _context: object) -> list[int]:
    return [os.getpid()]


def _double(snapshot: Snapshot, chunk: list[int], context: int) -> list[int]:
    return [len(snapshot.files) + item * context for item in chunk]


class GatherTests(TestCase):
    def snapshot(self) -> Snapshot:
        return Snapshot(
            snapshot_id="s",
            root=Path(),
            policy_version="p",
            created_at="t",
            duration_ms=0,
            files=(),
            exclusions=(),
            events=(),
        )

    def test_a_chunk_whose_worker_died_is_recomputed_in_place(self) -> None:
        finished: Future[list[int]] = Future()
        finished.set_result([10, 20])
        died: Future[list[int]] = Future()
        died.set_exception(BrokenProcessPool("worker exited"))
        results = parallel.gather_in_order(
            self.snapshot(), [finished, died], [[5, 10], [3, 4]], _double, 2
        )
        self.assertEqual(results, [10, 20, 6, 8])

    def test_an_analyzer_error_is_raised_not_retried(self) -> None:
        # Only a pool failure is recoverable. A reader that raised would raise
        # serially too, and hiding it behind a retry would run it twice.
        failed: Future[list[int]] = Future()
        failed.set_exception(KeyError("bug"))
        with self.assertRaises(KeyError):
            parallel.gather_in_order(self.snapshot(), [failed], [[1]], _double, 1)


class ChunkTests(TestCase):
    def test_chunks_concatenate_back_to_the_input(self) -> None:
        items = list(range(37))
        for count in (1, 2, 5, 37, 100):
            with self.subTest(count=count):
                chunks = parallel.chunk_by_weight(items, [item % 7 for item in items], count)
                self.assertEqual([item for chunk in chunks for item in chunk], items)
                self.assertLessEqual(len(chunks), min(count, len(items)))
                self.assertTrue(all(chunks))

    def test_weight_balances_the_chunks(self) -> None:
        # One heavy file first, then many light ones: it gets a chunk to itself.
        weights = [1000] + [10] * 100
        chunks = parallel.chunk_by_weight(list(range(101)), weights, 2)
        self.assertEqual(chunks[0], [0])

    def test_degenerate_inputs(self) -> None:
        self.assertEqual(parallel.chunk_by_weight([], [], 4), [])
        self.assertEqual(parallel.chunk_by_weight(["a"], [0], 4), [["a"]])
        self.assertEqual(parallel.chunk_by_weight(["a", "b"], [0, 0], 0), [["a", "b"]])


class ResolveJobsTests(TestCase):
    def test_explicit_counts_are_taken_as_given(self) -> None:
        self.assertEqual(parallel.resolve_jobs(1), 1)
        self.assertEqual(parallel.resolve_jobs(3), 3)

    def test_automatic_uses_the_cores_up_to_a_ceiling(self) -> None:
        with mock.patch.dict(os.environ, {parallel.JOBS_ENVIRONMENT: ""}):
            automatic = parallel.resolve_jobs(0)
            self.assertEqual(parallel.resolve_jobs(None), automatic)
        self.assertGreaterEqual(automatic, 1)
        self.assertLessEqual(automatic, parallel.MAX_DEFAULT_JOBS)

    def test_the_environment_sets_the_automatic_value_only(self) -> None:
        with mock.patch.dict(os.environ, {parallel.JOBS_ENVIRONMENT: "3"}):
            self.assertEqual(parallel.resolve_jobs(0), 3)
            self.assertEqual(parallel.resolve_jobs(1), 1)

    def test_an_unreadable_environment_value_is_not_an_error(self) -> None:
        with mock.patch.dict(os.environ, {parallel.JOBS_ENVIRONMENT: "many"}):
            self.assertGreaterEqual(parallel.resolve_jobs(0), 1)

    def test_a_negative_count_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            parallel.resolve_jobs(-1)

    def test_a_worker_never_starts_workers_of_its_own(self) -> None:
        with mock.patch("multiprocessing.parent_process", return_value=object()):
            self.assertEqual(parallel.resolve_jobs(8), 1)
