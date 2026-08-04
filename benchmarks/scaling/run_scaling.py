# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import json
import tempfile
import time
import tracemalloc
from pathlib import Path

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.scanner import scan_repository


def measure(file_count: int, lines_per_file: int = 100) -> dict[str, int]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for index in range(file_count):
            (root / f"module_{index:05d}.py").write_text(
                "value = 1\n" * lines_per_file,
                encoding="utf-8",
            )
        tracemalloc.start()
        started = time.perf_counter()
        snapshot = scan_repository(root)
        analysis = analyze_snapshot(snapshot)
        total_ms = round((time.perf_counter() - started) * 1000)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return {
            "files": file_count,
            "lines": snapshot.total_lines,
            "total_ms": total_ms,
            "python_peak_allocated_bytes": peak,
            "claims": len(analysis.claims),
            "evidence": len(analysis.evidence),
        }


if __name__ == "__main__":
    print(json.dumps([measure(size) for size in (100, 500, 1_000)], indent=2))
