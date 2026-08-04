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
            snapshot = scan_repository(root)
            result = analyze_snapshot(snapshot)
            duration = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            self.assertEqual(len(snapshot.files), 300)
            self.assertEqual(result.coverage[0].analyzed_files, 300)
            self.assertLess(duration, 10.0)
            self.assertLess(peak, 64 * 1024 * 1024)
