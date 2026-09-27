# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Worker processes for analysis, with output identical to a serial run.

Every reader in this engine is pure Python and CPU-bound, so threads cannot
help: the interpreter lock serialises them. Processes can, and on a repository
the size of Django the Python reader alone was forty-five seconds of one core
while three others sat idle.

Parallelism is allowed to change how long a run takes and nothing else. Two
rules keep it that way:

- Work is split, never reordered. A caller hands over an ordered sequence and
  gets results back in that order however the workers finished, so every merge
  downstream sees exactly what a serial loop would have produced. The ledger,
  the exports and the specification are byte-identical at any worker count,
  and `tests/test_parallel.py` holds them to it.
- A pool that cannot run is not a verdict. If worker processes cannot be
  started or die mid-run, the same work runs serially in this process. The
  run is slower, never different and never lost.

Workers are started with `spawn` on every platform. It is the only method
Windows offers, and on POSIX `fork` copies whatever threads the parent holds --
an MCP server's event loop, a dashboard's socket -- into a child that cannot
run them. One behaviour is uniform everywhere as a result.

Nothing here reads or executes the analyzed repository beyond what the serial
readers already do: a worker imports this engine's own modules and reads the
same bounded, hash-checked files a serial run would.
"""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Callable, Sequence
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

from open_skeleton.models import Snapshot

JOBS_ENVIRONMENT = "OPEN_SKELETON_JOBS"

# Past eight workers the merge in the parent, which is serial by design, is
# most of what remains; more processes only add startup cost and memory.
MAX_DEFAULT_JOBS = 8

# Below this much eligible source a pool costs more to start than it saves.
# Spawning a worker imports the engine afresh, about a tenth of a second each,
# and the fixtures a test suite or a small service consists of analyze faster
# than that serially.
MIN_PARALLEL_BYTES = 1_500_000

# Several chunks per worker so one slow file does not leave the rest idle at
# the end of a run; few enough that per-task overhead stays negligible.
CHUNKS_PER_WORKER = 4

# The snapshot a worker was started with, set once by the pool initializer.
_WORKER_STATE: dict[str, Snapshot] = {}


def resolve_jobs(requested: int | None) -> int:
    """The worker count a run should use: explicit, from the environment, or auto.

    `0` and `None` mean automatic. A caller asking for more than one worker
    from a script must keep its entry point under `if __name__ == "__main__":`,
    as `spawn` requires of every multiprocessing program. An unreadable environment value is treated
    as automatic rather than as an error, because a stray variable in somebody's
    shell is not a reason for an analysis to refuse to run.
    """

    if requested is None or requested == 0:
        configured = os.environ.get(JOBS_ENVIRONMENT, "").strip()
        try:
            requested = int(configured) if configured else 0
        except ValueError:
            requested = 0
    if requested < 0:
        raise ValueError("jobs must be zero (automatic) or a positive worker count")
    # A worker never starts workers of its own. `spawn` re-imports the parent's
    # main script in every child, and a script that calls this without an
    # `if __name__ == "__main__":` guard would otherwise start a pool from
    # each of them in turn. Serial here bounds that mistake to wasted time.
    if multiprocessing.parent_process() is not None:
        return 1
    if requested == 0:
        return max(1, min(os.cpu_count() or 1, MAX_DEFAULT_JOBS))
    return requested


def worth_parallelising(snapshot: Snapshot, jobs: int) -> bool:
    return jobs > 1 and snapshot.total_bytes >= MIN_PARALLEL_BYTES


def _initialise_worker(snapshot: Snapshot) -> None:
    _WORKER_STATE["snapshot"] = snapshot


def worker_snapshot() -> Snapshot:
    try:
        return _WORKER_STATE["snapshot"]
    except KeyError:
        raise RuntimeError("worker was started without a snapshot") from None


def start_pool(snapshot: Snapshot, jobs: int) -> ProcessPoolExecutor:
    """A pool whose workers each hold the snapshot, sent once rather than per task."""

    return ProcessPoolExecutor(
        max_workers=jobs,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_initialise_worker,
        initargs=(snapshot,),
    )


def chunk_by_weight[ItemT](
    items: Sequence[ItemT], weights: Sequence[int], chunk_count: int
) -> list[list[ItemT]]:
    """Consecutive runs of `items` of roughly equal total weight, order kept.

    Consecutive rather than interleaved so concatenating the chunks gives back
    the input exactly; that is what lets a merge stay identical to a serial run.
    """

    if not items:
        return []
    chunk_count = max(1, min(chunk_count, len(items)))
    total = sum(max(1, weight) for weight in weights)
    target = total / chunk_count
    chunks: list[list[ItemT]] = [[]]
    running = 0.0
    for item, weight in zip(items, weights, strict=True):
        if chunks[-1] and running >= target * len(chunks) and len(chunks) < chunk_count:
            chunks.append([])
        chunks[-1].append(item)
        running += max(1, weight)
    return chunks


ChunkFunction = Callable[[Snapshot, list[Any], Any], list[Any]]


def _call_in_worker(function: ChunkFunction, chunk: list[Any], context: Any) -> list[Any]:
    return function(worker_snapshot(), chunk, context)


def submit_chunks(
    executor: Executor,
    function: ChunkFunction,
    chunks: list[list[Any]],
    context: Any,
) -> list[Future[list[Any]]]:
    """Queue `function(snapshot, chunk, context)` for each chunk.

    `function` must be defined at module scope so a worker can import it.
    """

    return [executor.submit(_call_in_worker, function, chunk, context) for chunk in chunks]


def gather_in_order(
    snapshot: Snapshot,
    futures: list[Future[list[Any]]],
    chunks: list[list[Any]],
    function: ChunkFunction,
    context: Any,
) -> list[Any]:
    """Every chunk's results, concatenated in submission order.

    A chunk whose worker died is recomputed here, serially, with the same
    function. The outcome is the same either way; only the time differs.
    """

    results: list[Any] = []
    for future, chunk in zip(futures, chunks, strict=True):
        try:
            results.extend(future.result())
        except (BrokenProcessPool, OSError, EOFError):
            results.extend(function(snapshot, chunk, context))
    return results
