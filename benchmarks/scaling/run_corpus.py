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
from open_skeleton.reuse import ReadCache
from open_skeleton.scanner import _snapshot_id, scan_repository

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


def _fingerprint_of(result: Any, where: Path) -> str:
    """The exported bytes of one analysis, hashed the same way as the cold run."""

    where.mkdir(parents=True, exist_ok=True)
    export_analysis_jsonl(result, where / "analysis.jsonl")
    export_analysis_markdown(result, where / "analysis.md")
    return hashlib.sha256(
        (where / "analysis.jsonl").read_bytes() + (where / "analysis.md").read_bytes()
    ).hexdigest()


def _largest_entry(cache: ReadCache) -> tuple[str, ...] | None:
    """The cached file with the most records, which is the one worth re-reading.

    Dropping an entry stands in for a file whose bytes changed, and the
    largest is chosen so the measurement is of the worst realistic case rather
    than of a file nothing reads.
    """

    def weight(key: tuple[str, ...]) -> int:
        outcome = cache.entries[key]
        return sum(
            len(getattr(outcome, name, ())) for name in ("symbols", "edges", "evidence", "claims")
        )

    return max(cache.entries, key=weight) if cache.entries else None


def _snapshot_without_a_source_file(snapshot: Any) -> Any:
    """The same snapshot with one file no reader of source reads removed.

    Used to move a run onto a different snapshot id without touching the
    repository. The file dropped is one no language reader claims, so the set
    of module names is unchanged and every source file stays reusable; the
    snapshot id digests the whole inventory, so it changes regardless.

    The id is recomputed with the scanner's own function rather than a copy of
    the rule. A private copy of a rule goes stale the moment the rule moves,
    which is how an earlier instrument in this repository ended up reporting a
    rate that fell as the engine improved.
    """

    # Anything the Python reader does not read, and not a package marker,
    # whose presence decides what its siblings are called. A repository that
    # is nothing but Python has no such file, and this returns nothing rather
    # than dropping a module and reporting the resulting full re-read as
    # reuse.
    droppable = [
        item
        for item in snapshot.files
        if item.language != "Python" and not item.path.endswith("__init__.py")
    ]
    if not droppable or len(snapshot.files) < 2:
        return None
    dropped = max(droppable, key=lambda item: item.path)
    kept = [item for item in snapshot.files if item.path != dropped.path]
    return replace(snapshot, files=tuple(kept), snapshot_id=_snapshot_id(kept))


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
        exported = hashlib.sha256(
            (workspace / "analysis.jsonl").read_bytes() + (workspace / "analysis.md").read_bytes()
        ).hexdigest()
        # Taken before the re-save, so a run with `--no-resave` -- the only
        # kind an older revision can finish on a large repository -- is
        # compared with exactly what this run wrote the first time.
        fingerprint = _ledger_fingerprint(workspace / "evidence.sqlite3")
        # Reuse, held to the same bar as workers: it may change how long a run
        # takes and nothing else.
        #
        # Two runs, because they answer different questions. The first warms a
        # cache and analyzes again with every file reusable, which is the
        # ceiling and proves the merge is unchanged when no read happened at
        # all. The second drops the largest file's entry and analyzes again, so
        # exactly one file is read and the rest reused -- the shape of a
        # one-line edit, without writing to the repository being measured. If
        # either answers differently from the cold run above, this fails.
        cache = ReadCache()
        analyze_snapshot(snapshot, cache=cache, **options)
        started = time.perf_counter()
        warm = replace(analyze_snapshot(snapshot, cache=cache, **options), duration_ms=0)
        timed("reuse_all_s", started)
        stages["reuse_hits"] = float(cache.hits)

        evicted = _largest_entry(cache)
        if evicted is not None:
            cache.entries.pop(evicted)
        cache.hits = cache.misses = 0
        started = time.perf_counter()
        partial = replace(analyze_snapshot(snapshot, cache=cache, **options), duration_ms=0)
        timed("reuse_one_read_s", started)
        stages["reuse_one_read_misses"] = float(cache.misses)

        reuse_exports = _fingerprint_of(warm, workspace / "reuse")
        partial_exports = _fingerprint_of(partial, workspace / "partial")

        # Both runs above analyzed the snapshot the cache was filled from, so
        # every identifier and timestamp a reused record carries was already
        # the right one and rebinding them was a no-op. That is not the case a
        # cache exists for, so it is not the case to prove: this crosses a
        # snapshot boundary, where a reused record has to be renamed for the
        # run reusing it, and a mistake there would have shown up in neither
        # measurement above.
        #
        # A file no reader of source reads is what moves: the snapshot id
        # digests the whole inventory and changes, while the set of module
        # names the Python reader classifies calls against does not, so every
        # Python file is still reusable and the rebinding is what is under
        # test. Nothing is written to the repository being measured.
        across = _snapshot_without_a_source_file(snapshot)
        across_exports: str | None = None
        if across is not None:
            cache.hits = cache.misses = 0
            moved = replace(analyze_snapshot(across, cache=cache, **options), duration_ms=0)
            stages["reuse_across_snapshots_hits"] = float(cache.hits)
            across_exports = _fingerprint_of(moved, workspace / "across")
            cold_across = replace(analyze_snapshot(across, **options), duration_ms=0)
            stages["reuse_across_snapshots_cold_matches"] = float(
                across_exports == _fingerprint_of(cold_across, workspace / "across-cold")
            )

        resaved: str | None = None
        if resave:
            started = time.perf_counter()
            ledger.save_analysis(replace(result, created_at="2026-01-02T00:00:00.000+00:00"))
            timed("resave_s", started)
            resaved = _ledger_fingerprint(workspace / "evidence.sqlite3")
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
        "reuse_all_exports_sha256": reuse_exports,
        "reuse_one_read_exports_sha256": partial_exports,
        "reuse_across_snapshots_exports_sha256": across_exports,
        "ledger_sha256": fingerprint,
        "ledger_after_resave_sha256": resaved,
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
        for row in measured:
            # Reuse is held to the cold run of the same worker count: a run
            # that reused every file, and a run that read one file and reused
            # the rest, must both export exactly what reading everything did.
            for key in ("reuse_all_exports_sha256", "reuse_one_read_exports_sha256"):
                if row[key] != row["exports_sha256"]:
                    mismatches.append(
                        f"{root.name}: {key} differs from a cold run at jobs={row['jobs']}"
                    )
            if row.get("reuse_across_snapshots_cold_matches") == 0.0:
                mismatches.append(
                    f"{root.name}: reusing across a snapshot boundary answered "
                    f"differently from a cold run at jobs={row['jobs']}"
                )
            if row.get("reuse_one_read_misses") not in (None, 1.0):
                mismatches.append(
                    f"{root.name}: dropping one cached file read "
                    f"{row['reuse_one_read_misses']:.0f} files at jobs={row['jobs']}"
                )
        for row in measured[1:]:
            for key in ("exports_sha256", "ledger_sha256", "ledger_after_resave_sha256"):
                if row[key] != reference[key]:
                    mismatches.append(f"{root.name}: {key} differs at jobs={row['jobs']}")
    print(json.dumps({"runs": rows, "mismatches": mismatches}, indent=2))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
