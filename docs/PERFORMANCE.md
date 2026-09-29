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

## What a one-line edit costs, and what it could cost

`benchmarks/scaling/run_incremental_ceiling.py` analyzes a repository, adds one
line to its largest source file, analyzes it again, and compares every record
from the second run with every record from the first. Since per-file facts are
named by `content_key`, what it reports is the reuse available rather than a
projection of it.

September 29, 2026, on Windows AMD64, Python 3.12.14:

| Repository | Files | Evidence | Symbols | Edges | Claims | Whole-repository |
|---|---:|---:|---:|---:|---:|---:|
| open-skeleton | 183 | 95.1% | 95.9% | 94.8% | 0% | 5 |
| pygments | 339 | 98.6% | 97.5% | 98.6% | 0% | 4 |
| mypy | 949 | 96.0% | 99.1% | 96.1% | 0% | 4 |

Every record that is not reusable is traceable to a file that changed, with one
exception -- the last column. Four or five evidence records per repository have
`.` for a path and the snapshot id for an `excerpt_sha256`: `snapshot_census`,
`static_import_census`, `unsafe_census`, `rust_test_census`. Their excerpt is
the file inventory itself, so they change whenever any file does. They are the
whole class of reader output an incremental run cannot reuse, and they are
cheap to recompute.

Claims at zero is the design rather than a gap. A claim id hashes the snapshot,
the category and the text but not the path, so that two files stating the same
thing merge into one claim carrying both receipts; keying a claim by content
would produce two claims nothing can fold. About a thousand claims per
repository are rewritten each run against a few hundred thousand receipts that
are not.

Two things are worth recording about how these numbers were arrived at, because
both were wrong first.

An earlier version of this table read 99.5% to 100% everywhere. It was measured
by simulation, with `stable_id` wrapped to drop the snapshot id, and the
simulation keyed a receipt by its position in a file rather than by the file's
bytes. Appending a line moved nothing, so the edited file's own 1,287 receipts
counted as reusable. Under content keying they are correctly renamed, and the
honest rate is four points lower. The reuse worth having is the one that cannot
serve a receipt for bytes that changed.

And an edge depends on two files, not one. A resolved edge carries the
identifier of the symbol it lands on, that identifier follows the target file's
bytes, so editing a file renames every edge pointing into it from anywhere
else. Editing this repository's Python reader moved 76 edges declared in other
files, every one resolving into the edited one. An incremental implementation
keying an edge on its source file alone would keep all 76 and serve a
resolution to a symbol that no longer exists under that name.

```bash
python benchmarks/scaling/run_incremental_ceiling.py -- <repository>
```

### What one change makes untrue

Reuse is only correct for facts that depend on the file they were read from
and nothing else. `benchmarks/scaling/run_reader_locality.py` makes one change
at a time -- edit a file's bytes, add a file, remove a file -- and asks every
reader what it now says about the files that did **not** change.

| Change | Evidence | Symbols | Edges | Claims |
|---|---|---|---|---|
| edit one file | local | local | local | local |
| add a file | local | local | local | 2 census claims went |
| remove a file | local | **1 changed under the same identifier** | local | 2 census claims came |

So a cache may reuse an unchanged file's evidence, symbols and edges when a
file is edited or added, and may not when a file is removed. The one symbol
that moved is the case worth stating: its identifier is the bytes of
`tests/test_python_analyzer.py`, which did not change, so a cache keyed on
identity would have hit. Its `external_calls` metadata records where each call
lands, and a call into a module the snapshot no longer holds is reclassified
from `this repository` to `dependency`. Same name, different answer.

Two further dependencies are already recorded above: a resolved edge depends on
the file it lands in as well as the file it was read from, and the four or five
census receipts depend on the whole inventory.

The first version of this instrument deleted every second file rather than
making one realistic change. It found 4,306 Python receipts renamed and was
about to be read as "the Python reader is not per-file". The perturbation had
deleted `src/open_skeleton/__init__.py`, so package roots moved and every
qualified name in the repository lost its `open_skeleton.` prefix. A
measurement has to perturb the way a repository actually changes, or it reports
the shape of its own experiment.

```bash
python benchmarks/scaling/run_reader_locality.py -- <repository>
```

This enumeration is incomplete, and the instrument says so rather than
implying otherwise. On this repository the TypeScript, Java, C#, PowerShell and
Hum readers are exercised by no files, so they are reported as `none` and no
verdict about them has been reached. Running it over zod, a Java repository and
a C# repository would close that gap; a reader reported as `none` must not be
read as a reader reported as local.

### Reusing what a file already said

`analyze_snapshot(snapshot, cache=ReadCache())` reuses the Python reader's
per-file outcomes instead of reading the files again. September 29, 2026, on
Windows AMD64, Python 3.12.14, one worker:

| Repository | Files | Cold analysis | Every file reused | One file read, the rest reused |
|---|---:|---:|---:|---:|
| open-skeleton | 186 | 2.89 s | 0.93 s | 1.18 s |
| pygments | 339 | 3.19 s | 0.62 s | 0.70 s |
| mypy | 949 | 6.75 s | 3.59 s | 4.33 s |

An entry is keyed on every input the reader was given: its version, the file's
path and content hash, the module name the file was given, and a digest of
every module name in the snapshot. The last is there because it is an input
rather than context -- `external_calls` classifies a call by whether what it
lands in is a module of this repository -- and without it a cache hits after a
module is removed and serves a classification that is no longer true.

The Python reader is the only one that reuses anything. It is the one with a
per-file outcome type, introduced for parallel reading and named in
[LANDSCAPE.md](LANDSCAPE.md) as the unit to cache, and on these repositories it
is most of the reading. The other ten still run whole; none exceeded 2.1 s on
the repositories measured.

What is left after reuse is cross-file work, and it does not shrink: import and
call resolution over 91,598 edges on mypy, the censuses, ownership, and claim
merging and scoping. That is why mypy halves rather than vanishing while
pygments, which is entirely Python and has a fifth of the edges, drops
fivefold.

`benchmarks/scaling/run_corpus.py` fails if any of it answers differently. Each
reused run is fingerprinted as both the ledger rows it writes and the bytes it
exports, and compared with a cold run of the same snapshot. The exports alone
would be the weaker check: an export is a projection, and a field it does not
carry could differ while the two files stayed identical.

Three runs are compared: every file reused, one file read with the rest reused,
and -- the case that matters -- a run across a snapshot boundary, where a
reused record has to be renamed for the run reusing it. The first two analyze
the snapshot the cache was filled from, so renaming is a no-op there and a
mistake in it would pass unnoticed. Deleting the claim re-minting and re-running
produced a mismatch from the third alone.

```bash
python benchmarks/scaling/run_corpus.py --jobs 1 4 -- <repository>
```

### The store, and what it is worth on the command line

`analyze` and `scripts/turn_gate.py` keep the cache in
`read-cache.sqlite3` beside the ledger, one row per file, each row deflated.
`--no-reuse` reads everything again, which is how the two are compared.

On mypy, one worker, alternating the two so neither gets a quieter machine:

| | Median of four |
|---|---:|
| Reusing | 8.54 s |
| `--no-reuse` | 12.21 s |

Measured separately rather than interleaved, the same pair read 29.7 s and
23.5 s, in that order -- reuse apparently slower than no reuse. Both figures
were load, not reuse: everything on that machine was about twice its usual
speed for the minutes those runs took. A ratio between two numbers measured at
different times is not a ratio, and on a machine doing anything else the only
honest way to compare two configurations is to alternate them.

The store holds mypy at 23.1 MB. It was 106.7 MB before each row was deflated,
which for a tool that keeps a copy per analysed repository is the difference
between a cache and a liability; `zlib` at level 1 costs a fraction of the
write and returns four fifths of the disk.

### What a cache that outlived its process would cost

The cache lives as long as the caller holds it, so a library caller or the MCP
server reuses across analyses and the command line reuses nothing: each
invocation starts empty. That makes the agent-loop case -- `turn_gate.py` run
between turns, a fresh process each time -- the one case that gets no benefit,
which is the case the rest of this document argues matters most.

Whether that is worth fixing is a question about the cost of reloading, so it
was measured before anything was built. Encoding mypy's 220,211 Python records
as JSON and reading them back:

| Step | Time | Size |
|---|---:|---:|
| Cold analysis, for comparison | 8.73 s | |
| Encode to JSON | 1.33 s | 115.5 MB |
| Compress (gzip, level 1) | 0.80 s | 31.3 MB |
| Decompress | 0.25 s | |
| Decode and rebuild records | 0.99 s | |

Reloading costs about 1.6 s against 8.7 s of reading, so it is worth having.
Writing costs about 2.1 s a run, which says the store should rewrite the
entries that changed rather than all of them -- one row per file in SQLite, the
way the ledger already works, rather than one document rewritten each time.

One hazard turned up in the measurement and would not have turned up in the
design. Records survive a JSON round trip exactly, except symbols: a symbol's
`metadata` is free-form, `state_fields` holds tuples inside it, and JSON
returns them as lists, so 2 of this repository's 4,345 Python symbols came back
unequal. Nothing persisted can see the difference -- the ledger stores metadata
through `json.dumps` already, and `spec/diagrams.py` coerces the values back
with `tuple(item)` for exactly that reason -- but the in-memory records differ,
and a cache that returns a record unequal to the one a cold run would build is
the thing the oracle exists to reject. A store has to preserve tuples or the
reader has to stop producing them inside metadata; the measurement does not
decide which.

Those rates say a read cache would nearly always hit. They do not say the run
would get much faster, and the stage table is why. On mypy, on the same
machine and day:

| Stage | Serial | 4 workers |
|---|---:|---:|
| Scan | 7.45 s | 0.18 s |
| Readers and cross-reader passes | 10.30 s | 6.46 s |
| Ledger write | 7.29 s | 8.82 s |
| JSONL and Markdown export | 1.52 s | 1.88 s |
| **Total** | **26.55 s** | **17.34 s** |

With four workers the ledger write is already larger than everything the cache
would skip. A read cache that hit perfectly would take this run from 17.3 s to
about 11 s, not to nothing. That is worth having and it is not the headline the
word "incremental" usually implies, so it is recorded here before the work
rather than after it.

It also rules out one way of building it. If each fact were bound to a snapshot
by its own row, an incremental run would still write one row per fact. Inserting
mypy's 219,332 evidence, symbol and edge bindings into a two-column table with a
composite primary key takes 11.77 s -- more than the 8.82 s full write it was
meant to replace. Binding at file level instead, one row per file, takes 0.006 s
for the same snapshot. A fact belongs to a snapshot exactly when the file it was
read from is in that snapshot with those bytes, so the file list is a complete
binding and a per-fact one is redundant as well as slower.

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
  content hash, and binding them to snapshots by file rather than by fact,
  would make incremental analysis correct by construction; the per-file outcome
  records the parallel readers return are the natural unit to cache. How much
  that would save, and why it does not save as much as it sounds, is measured
  above.
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
