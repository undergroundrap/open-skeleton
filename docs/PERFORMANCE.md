# Performance and complexity

## Real repositories, end to end

`benchmarks/scaling/run_corpus.py` runs what `open-skeleton analyze` runs --
scan, every reader, the cross-reader passes, the ledger write and the exports --
in a fresh interpreter per measurement, and reports each stage separately. It
fingerprints the ledger rows and the exported bytes with the clock pinned, and
fails if any worker count produces a different fingerprint from the serial run.

September 27, 2026, on a 4-vCPU Linux container (Intel Xeon, 2.1 GHz), Python
3.12.3. "Before" is commit `38a4ba9`, measured by the same script against that
revision's sources.

| Repository (revision) | Files | Lines | Before | Serial now | 2 workers | 4 workers |
|---|---:|---:|---:|---:|---:|---:|
| SINGLE-PLAYER-AI-MUD (`93ebd51`) | 29 | 16,438 | 2.17 s | 1.52 s | 1.61 s | 1.58 s |
| urllib3 (`ed0ed07`) | 152 | 44,089 | 3.36 s | 2.29 s | 1.87 s | 1.61 s |
| clap (`6cde738`) | 632 | 112,508 | 5.58 s | 4.75 s | 3.79 s | 3.62 s |
| zod (`2bf7b06`) | 680 | 139,941 | 8.96 s | 7.48 s | 6.26 s | 5.56 s |
| Django (`9e06baf`) | 5,698 | 1,116,514 | 101.09 s | 60.64 s | 43.61 s | 34.53 s |

Every row at every worker count produced the same ledger and export
fingerprints as the serial run, and those are the fingerprints the original
revision produces: the output is byte-identical to what it was before any of
this work. The fixture is below the 1.5 MB threshold at
which a pool is started, so its worker columns are serial runs; the difference
between them is noise.

Where the time goes on Django:

| Stage | Before | Serial now | 4 workers |
|---|---:|---:|---:|
| Scan | 2.35 s | 1.52 s | 1.59 s |
| Readers and cross-reader passes | 71.74 s | 39.63 s | 13.80 s |
| Ledger write | 17.59 s | 13.72 s | 13.71 s |
| JSONL and Markdown export | 9.41 s | 5.78 s | 5.43 s |
| **Total** | **101.09 s** | **60.64 s** | **34.53 s** |

With four workers the readers now take about as long as the ledger write. That write is serial by
design -- one transaction, so a failed run leaves nothing half-recorded -- and is
the next thing to make faster; see "Where the remaining time is" below.

### Saving the same analysis twice

Re-analysing an unchanged repository produces the same content-addressed
identifiers, so the second save updates rows that already exist. It used to
replace them, and each replacement fired a foreign-key cascade that scanned an
unindexed column:

| Repository | First save | Second save before | Second save now |
|---|---:|---:|---:|
| urllib3 | 0.51 s | 61.48 s | 0.36 s |
| clap | 1.31 s | 403.64 s | 0.94 s |
| Django | 17.59 s | not measured (quadratic) | 10.75 s |

The same cascade deleted `claim_evidence` rows for any earlier claim that cited
a replaced receipt, leaving it `verified` with nothing behind it.
`tests/test_ledger_and_exports.py` fails on the old write path for that reason.

### Memory

Peak resident memory of the measuring process on Django was 1,090 MiB before
and 1,124 MiB serially now; with four workers it was 1,156 MiB for the parent.
Sampling the parent and its workers together every 50 ms found at most 830 MiB
in flight at once, which is a lower bound -- a sample can fall between peaks.
Workers hold one chunk of files and the snapshot, not the repository's records,
so memory does not multiply by the worker count in practice; the threat model
still states the bound as though it could.

### Reproduce

```bash
python benchmarks/scaling/run_corpus.py --jobs 1 2 4 -- <repository> [<repository> ...]
# the same measurement against an earlier revision, for a before/after pair
PYTHONPATH=<old-checkout>/src python benchmarks/scaling/run_corpus.py --jobs 1 --no-resave -- <repository>
```

`--no-resave` exists because the second save is quadratic on revisions before
the upsert fix and would dominate any run over a large repository.

## What changed, in order of effect

1. **Worker processes for per-file reading.** Python, TypeScript and Rust files
   are split into consecutive chunks of roughly equal bytes and read in a
   `spawn` pool; the other readers run whole in the same pool. Results are
   merged in the parent in file order, so every downstream sort, merge and
   identifier sees exactly what a serial loop produces. A pool that cannot
   start, or a worker that dies, falls back to serial reading of the affected
   chunk. See `src/open_skeleton/parallel.py`.
2. **The orphan-module census.** It asked, for each Python module, whether any
   import edge started with its name -- modules times imports, 18.1 s of the
   Django run. It now builds the set of every dotted prefix an import reaches
   once, and takes 78 ms.
3. **AST traversal.** `ast.NodeVisitor` builds `"visit_" + class name` and
   looks it up for every node; `ast.walk` reaches children through two
   generators per node. `FastNodeVisitor` resolves the handler once per node
   type and `walk` inlines the child loop. Seventeen per-module extractors also
   stopped iterating every node to discard most of them: each reads a per-tree
   index of nodes by type, in walk order. Order, dispatch and selection are
   held to node-for-node identity with the standard library over this
   repository's own sources by `tests/test_ast_visitor.py`.
4. **Ledger upserts** for evidence, symbols and edges, and a larger page cache
   for the duration of the write.
5. **Exports** encode each record's fields directly rather than through
   `asdict`, which deep-copied every nested structure, and reuse one encoder.

## Where the remaining time is

- **Ledger write (14 s on Django).** Close to the cost of inserting that many
  rows into this schema: 64-character text primary keys, each table stored as a
  rowid table plus a separate primary-key index, plus secondary indexes and
  foreign-key checks. Integer surrogate keys or `WITHOUT ROWID` tables would
  cut it, and need a backward-compatible migration, which is why this change
  does not attempt one.
- **Re-reading unchanged files.** Every evidence and symbol identifier embeds
  the snapshot id, so a one-line edit produces a new identifier for every fact
  in the repository and nothing can be reused. Keying per-file facts by file
  content hash, and binding them to snapshots separately, would make
  incremental analysis correct by construction; the per-file outcome records
  the parallel readers return are the natural unit to cache.
- **Readers not yet split.** Java, C#, PowerShell, SQL and the project-metadata
  reader run whole in one worker each. None exceeded 2.1 s on the repositories
  above.

## Synthetic scaling (historical)

Synthetic measurements use 100-line Python files containing one module
assignment per line. They include scanning, hashing, AST analysis, cross-adapter
analysis, and in-memory result construction; they exclude SQLite persistence and
export.

August 4, 2026 local measurements:

| Files | Lines | Total time | Traced Python peak |
|---:|---:|---:|---:|
| 100 | 10,000 | 656 ms | 11,394,014 bytes |
| 500 | 50,000 | 3,919 ms | 56,947,348 bytes |
| 1,000 | 100,000 | 8,425 ms | 113,010,185 bytes |

The observed memory ratio from 10k to 100k lines was 9.92x; the time ratio was
12.84x. This is consistent with the intended approximately linear semantic pass
plus allocation/index overhead, but three synthetic points are not an
asymptotic proof.

The CI smoke budget analyzes 300 files/30,000 lines in under 10 seconds and
below 64 MiB of traced allocations. Reproduce the broader measurement with:

```powershell
$env:PYTHONPATH = "src"
python benchmarks\scaling\run_scaling.py
```

Large repositories with many assignment/call sites create many receipts by
design. Future work includes streaming ledger writes and bounded per-adapter
retention so evidence volume does not require holding a whole snapshot's
records in memory.
