# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""End-to-end cost on real repositories, and proof that workers change nothing else.

The synthetic scaling run measures one reader on one shape of file. This runs
what `open-skeleton analyze` runs -- scan, every reader, the cross-reader
passes, the ledger write and the exports -- over repositories somebody wrote,
at each requested worker count, and reports each stage separately so a total
cannot hide where the time went.

Speed is only half the claim. For every repository the ledger rows and the
exported bytes are fingerprinted at each worker count, with the clock pinned,
and the run fails if any fingerprint differs from the serial one. A faster
engine that answers differently is a different engine.

It also saves each analysis a second time into the same ledger, because that
is what re-analysing an unchanged repository does, and that path was once
three hundred times slower than the first save.

    python benchmarks/scaling/run_corpus.py --jobs 1 4 -- <repo> [<repo> ...]

To compare with an earlier revision, run the same file against its sources:
`PYTHONPATH=<old-checkout>/src python run_corpus.py --jobs 1 -- <repo>`.

Each measurement runs in a fresh interpreter, so no run inherits another's
caches or memory high-water mark. Peak memory is reported twice: the
operating system's resident-set high-water mark for the measuring process, and
-- on Linux, where `/proc` allows it -- the largest sum of that process and its
workers, sampled every 50 ms. The first alone understates a parallel run; the
kernel's own figure for children cannot be used, because a spawned worker
inherits its parent's high-water mark in the instant between fork and exec.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import pkgutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import open_skeleton
from open_skeleton import models
from open_skeleton.analysis import analyze_snapshot
from open_skeleton.exports import export_analysis_jsonl, export_analysis_markdown
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository

try:
    import resource
except ImportError:  # Windows: no resident-set figures, everything else runs.
    resource = None  # type: ignore[assignment]

PINNED_CLOCK = "2026-01-01T00:00:00.000+00:00"


def _pin_clock() -> None:
    """Every module's `utc_now`, pinned, so two runs can be compared byte for byte.

    Module scope as well as call scope: a spawned worker re-imports this script
    under another name, runs this at import, and so pins its own copy.
    """

    def pinned() -> str:
        return PINNED_CLOCK

    models.utc_now = pinned
    for info in pkgutil.walk_packages(open_skeleton.__path__, "open_skeleton."):
        module = importlib.import_module(info.name)
        if hasattr(module, "utc_now"):
            module.utc_now = pinned  # type: ignore[attr-defined]


_pin_clock()


def _ledger_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with closing(sqlite3.connect(path)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite%' AND name NOT LIKE '%search_%' ORDER BY name"
            )
        ]
        for table in tables:
            rows = sorted(repr(row) for row in connection.execute(f"SELECT * FROM {table}"))  # noqa: S608
            digest.update(table.encode())
            for row in rows:
                digest.update(row.encode())
    return digest.hexdigest()


def _peak_mib() -> float | None:
    if resource is None:
        return None
    # Linux reports kilobytes and macOS bytes.
    scale = 1 if sys.platform == "darwin" else 1024
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale / 1024 / 1024, 1)


def _resident_kib(pid: int) -> int:
    try:
        with Path(f"/proc/{pid}/status").open(encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0


def _family_kib(parent: int) -> int:
    total = _resident_kib(parent)
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with Path(f"/proc/{entry.name}/stat").open(encoding="ascii") as stat:
                fields = stat.read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[1]) == parent:
            total += _resident_kib(int(entry.name))
    return total


class _FamilySampler:
    """The largest resident total of this process and its children, while it runs."""

    def __init__(self) -> None:
        self.peak_kib = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.available = sys.platform.startswith("linux")

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            self.peak_kib = max(self.peak_kib, _family_kib(os.getpid()))

    def __enter__(self) -> _FamilySampler:
        if self.available:
            self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self.available:
            self._thread.join()


def measure(root: Path, jobs: int, *, resave: bool = True) -> dict[str, Any]:
    stages: dict[str, float] = {}

    def timed(name: str, started: float) -> None:
        stages[name] = round(time.perf_counter() - started, 3)

    with tempfile.TemporaryDirectory() as temporary, _FamilySampler() as sampler:
        workspace = Path(temporary)
        begun = time.perf_counter()
        snapshot = replace(scan_repository(root), duration_ms=0, events=())
        timed("scan_s", begun)
        started = time.perf_counter()
        # Serial runs pass no worker count, so this script also measures
        # revisions from before there was one -- which is how a change is
        # compared with what it replaced, on the same machine.
        options: dict[str, Any] = {} if jobs == 1 else {"jobs": jobs}
        result = replace(analyze_snapshot(snapshot, **options), duration_ms=0)
        timed("analyze_s", started)
        ledger = EvidenceLedger(workspace / "evidence.sqlite3")
        started = time.perf_counter()
        ledger.save_snapshot(snapshot)
        ledger.save_analysis(result)
        timed("ledger_s", started)
        started = time.perf_counter()
        export_analysis_jsonl(result, workspace / "analysis.jsonl")
        export_analysis_markdown(result, workspace / "analysis.md")
        timed("export_s", started)
        stages["total_s"] = round(time.perf_counter() - begun, 3)
        if resave:
            started = time.perf_counter()
            ledger.save_analysis(replace(result, created_at="2026-01-02T00:00:00.000+00:00"))
            timed("resave_s", started)
        exported = hashlib.sha256(
            (workspace / "analysis.jsonl").read_bytes() + (workspace / "analysis.md").read_bytes()
        ).hexdigest()
        # Fingerprinted before the re-save would be ideal, but the re-save is
        # part of what is being checked: it must leave the same facts behind.
        fingerprint = _ledger_fingerprint(workspace / "evidence.sqlite3")
    return {
        "repository": root.name,
        "jobs": jobs,
        "files": len(snapshot.files),
        "lines": snapshot.total_lines,
        "symbols": len(result.symbols),
        "edges": len(result.edges),
        "evidence": len(result.evidence),
        "claims": len(result.claims),
        **stages,
        "peak_rss_mib": _peak_mib(),
        "peak_total_rss_mib": round(sampler.peak_kib / 1024, 1) if sampler.available else None,
        "exports_sha256": exported,
        "ledger_sha256": fingerprint,
    }


def _measure_in_fresh_process(root: Path, jobs: int, *, resave: bool) -> dict[str, Any]:
    command = [sys.executable, __file__, "--single", str(jobs), str(root)]
    if not resave:
        command.append("--no-resave")
    completed = subprocess.run(  # noqa: S603 -- this script, re-invoked
        command, capture_output=True, text=True, check=True, env=os.environ
    )
    measured: dict[str, Any] = json.loads(completed.stdout)
    return measured


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--single":
        jobs, root = int(sys.argv[2]), Path(sys.argv[3])
        print(json.dumps(measure(root, jobs, resave="--no-resave" not in sys.argv)))
        return 0

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, nargs="+", default=[1, 0])
    parser.add_argument(
        "--no-resave",
        action="store_true",
        help="Skip the second save; on revisions before the upsert fix it is quadratic.",
    )
    parser.add_argument("repositories", type=Path, nargs="+")
    arguments = parser.parse_args()

    rows = []
    mismatches = []
    for repository in arguments.repositories:
        root = repository.expanduser().resolve(strict=True)
        measured = [
            _measure_in_fresh_process(root, jobs, resave=not arguments.no_resave)
            for jobs in arguments.jobs
        ]
        rows.extend(measured)
        reference = measured[0]
        for row in measured[1:]:
            for key in ("exports_sha256", "ledger_sha256"):
                if row[key] != reference[key]:
                    mismatches.append(f"{root.name}: {key} differs at jobs={row['jobs']}")
    print(json.dumps({"runs": rows, "mismatches": mismatches}, indent=2))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
