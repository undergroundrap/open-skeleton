# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import time
import tracemalloc
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.scanner import scan_repository

# A budget is only worth having if going over it means this code got slower.
# Wall-clock time does not mean that: a hosted runner is a throttled VM on
# contended storage and took twenty-two seconds for work that takes a few here,
# and a test that goes red because some machine was busy teaches people to
# ignore red. That was met by exempting hosted runners, which left the budget
# measuring a quiet machine and going red on a busy one -- this repository's own
# gate failed three times in one afternoon while benchmarks ran beside it.
#
# So the budget is on processor time, which describes the code in the way the
# allocation ceiling below already does. On a quiet machine this work spends
# about 3.1 seconds of it and 3.3 of wall clock; most of both is `tracemalloc`,
# which roughly doubles the run and is the price of the allocation ceiling.
# Processor time is not immune to a loaded machine -- a run during a benchmark
# went over -- but it moves far less than wall clock, and a threefold margin is
# still a regression this would catch.
#
# A wall-clock ceiling stays beside it, loose enough to mean only one thing:
# something hung, or the machine is in no state to be running tests at all.
PROCESSOR_BUDGET_SECONDS = 10.0
HANG_CEILING_SECONDS = 180.0


class PerformanceSmokeTests(TestCase):
    def test_bounded_pipeline_stays_within_smoke_budget(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(300):
                (root / f"module_{index:04d}.py").write_text(
                    "value = 1\n" * 100,
                    encoding="utf-8",
                )

            tracemalloc.start()
            started = time.perf_counter()
            # Serial by default, so this process does the work and its
            # processor time is the work. A worker pool would spend its time
            # in children and not be counted here.
            spent = time.process_time()
            snapshot = scan_repository(root)
            result = analyze_snapshot(snapshot)
            processor = time.process_time() - spent
            duration = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            self.assertEqual(len(snapshot.files), 300)
            self.assertEqual(result.coverage[0].analyzed_files, 300)
            # Allocation is a property of the code and holds on any machine.
            self.assertLess(peak, 64 * 1024 * 1024)

            self.assertLess(
                processor,
                PROCESSOR_BUDGET_SECONDS,
                f"pipeline spent {processor:.1f}s of processor time against a "
                f"{PROCESSOR_BUDGET_SECONDS:.0f}s budget",
            )
            self.assertLess(
                duration,
                HANG_CEILING_SECONDS,
                f"pipeline took {duration:.1f}s of wall clock, which is not a slow "
                f"machine but something wrong",
            )
