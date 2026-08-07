# Benchmark methodology

## Fixture

- repository: `undergroundrap/SINGLE-PLAYER-AI-MUD`
- commit: `93ebd51cb4083d2307564c265394358e53c4f5ca`
- gold: `benchmarks/single-player-ai-mud/gold.json`
- baseline: supplied commercial Markdown export, SHA-256 pinned in the gold file

The gold set contains 33 source-grounded material claims covering API/client counts, concentration, persistence, state ownership, security, documentation drift, dependency drift, mathematics, testing, delivery, runtime topology, AI failures, and framework behavior. Scoring uses deterministic regex/category/status matchers plus current receipt-hash validation. No LLM judge determines the score.

## Metrics

- recall: weighted gold credit recovered
- scoped precision: matched emitted claims divided by all emitted claims in explicitly enumerated material categories
- evidence correctness: matched claims whose referenced receipts still validate and include an expected source path
- conflict detection: gold conflicts emitted as conflict with contradicting receipts
- time to first finding: wall time until the first analyzer returns claims
- total time: scan plus deterministic analysis
- peak memory: Python `tracemalloc` allocation peak, explicitly not process RSS
- output volume: characters, whitespace-delimited words, and physical lines in the concise Markdown projection

Baseline `hit`, `partial`, `incorrect`, and `miss` outcomes are manual source/artifact adjudications. Baseline precision is limited to statements mapped into the gold set; the million-character artifact has not been exhaustively sentence-labeled.

## Reproduce

```powershell
git clone https://github.com/undergroundrap/SINGLE-PLAYER-AI-MUD.git fixture
git -C fixture checkout 93ebd51cb4083d2307564c265394358e53c4f5ca
open-skeleton benchmark fixture `
  --gold benchmarks\single-player-ai-mud\gold.json `
  --output-dir benchmark-output
```

The command fails on a commit mismatch and writes `analysis.jsonl`, `analysis.md`, `benchmark.json`, and `benchmark.md`.

## Current local result

On the August 4, 2026 release-candidate run, Open Skeleton matched all 33 material claims with current receipts. It completed in 1.247 seconds, reached the first finding in 691 ms, and allocated a traced peak of 10,699,295 bytes. The concise report contained 4,086 words.

The supplied baseline artifact scored 89.4% material recall/scoped precision, 95.5% evidence correctness, and 75.0% conflict detection. It took approximately 20,820,000 ms and contained approximately 180,845 words.

## Limits

This is a useful regression fixture, not evidence of universal superiority. It was authored and adjudicated with repository-author knowledge, not audited by an external laboratory. More languages, architectures, repository sizes, and independent reviewers are required before broad claims.

## A measurement that was attempted and abandoned

Three benchmarks here compare a document against a baseline: fact coverage
compares the names both mention, the structure diff compares the subjects both
organise around, and the question set compares answers to questions whose
ground truth came from source. None of them answers the question a reader
actually asks, which is whether the two documents *reached the same
conclusions*.

That measurement was built three times and discarded three times. The record
is kept here so the next person does not spend the afternoon.

**Attempt one — negation near a matched term.** A specification that reports
absences well is full of the word *no*, so a negation test inside any useful
window fires almost everywhere. It reported 41% disagreement, nearly all of it
false: sections the baseline plainly covers at length were scored as absent
because an unrelated negation sat within three hundred characters.

**Attempt two — negation immediately before the term.** Tightening the window
to the phrase level did not separate them either. On the baseline used here
`Dockerfile` scores three immediate negations against fourteen plain mentions,
while the document states outright that no Dockerfile exists — because it also
lists `Dockerfile` among the paths it searched.

**Attempt three — presence only, using each section's own probe terms.** This
inverted the error. Probe terms are library names and file globs, because that
is what a probe queries; a baseline discusses the *concept* in English. So
Collection Pagination and Health and Readiness both read as never raised while
the baseline says "readiness" eight times and "liveness" fourteen.

The common cause is that comparing conclusions between two prose documents is
a semantic matching problem, and this engine's deterministic path has no model
in it by design. A keyword comparison can tell you what two documents *name*;
it cannot tell you what they *concluded*. Shipping a number that claims
otherwise would be the true-but-misleading failure `open-skeleton audit`
exists to catch, applied to our own benchmarks.

## Robustness across unseen packages

```
python benchmarks/robustness/run_robustness.py --root .venv/Lib/site-packages --strict
```

Fixtures are written by someone who already knows what the analyzer should
find. This runs it over every package in a directory instead and reports the
two failures a fixture suite cannot show.

A **crash** is the worst outcome available: the repository produces nothing at
all, and the cause is usually one statement. An installed library raised
`KeyError` on a module-level counter and abandoned the entire package, from a
shape — `global n` where `n` is not a mutable container — that appears in no
fixture anyone would think to write.

**Silence** is the quieter failure. A package that analyzes cleanly and says
nothing is not a simple package; it is a taxonomy with no category for what
that code contains. Two claims for `attrs` read as a quiet library and was a
vocabulary built entirely from web applications, with no notion of a public
surface or a scheduled removal. Census categories are excluded from the count,
since they are emitted for every repository whether or not anything is found.

Result on 68 installed packages, 61 of them with three or more source files:

| Measure | Result |
|---|---|
| Crashed | 0 |
| Analyzed but silent | 0 |
| Claim density | 0.14–4.22 per source file |
| Spread | 31× |

Spread is not by itself a defect. A Markdown parser has fewer architectural
facts than a CLI framework, and `platformdirs` scores highest because reading
environment settings is the entire job of the library — 30 recorded against 33
read sites in its source. The number to watch is the silent column: a package
that says nothing names a category that does not exist yet.

## Differential comparison against a real parser

```
npm install esbuild
python benchmarks/differential/run_differential.py --root some/typescript/project
```

Every other check here needs someone to imagine the input first. Fixtures are
written by a person who already knows the answer, and a corpus sweep only
covers shapes some repository happened to contain. A form nobody thought of
escapes both — `export { type Foo, Bar }` published an export named `type`
for exactly that reason.

A differential test needs no such imagination. Feed both readers the same file
and any disagreement is a lead. esbuild is the reference because it reports
what the module system actually binds, and the difference from what a
specification wants is the useful part:

| Disagreement | Meaning |
|---|---|
| esbuild reports it, we do not | **Defect** — the module exports it and the spec omits it |
| We report it, esbuild does not | Read it. Usually a type export, which esbuild erases and a specification needs |

First run against `zod`, 237 files: **10 files with missing exports**, in four
forms plus one tokenizer fault.

- `export * as core from "./x"` binds a namespace object; only the bare
  `export * from` names nothing.
- `export namespace errorUtil {}` — the keyword was not in the exportable set.
- `export const { GET } = handler()` — destructuring binds each name.
- `export default class Engine {}` binds `default`, not `Engine`. An importer
  writes `import Anything from "./x"`, so the claim that renaming the class
  breaks importers was simply false.
- A regex literal may contain a quote. Without a concept of regex literals the
  tokenizer read `/^[^\s@"]+$/` as opening a string and swallowed the rest of
  the file, losing every declaration after it.

After: **0 files with missing exports**, 32 with names esbuild erases, all of
them type exports.

esbuild is a development dependency and is never required to analyze a
repository. Without it the harness exits zero and says so, because a check
that cannot run is not a check that failed.

## Differential comparison for Rust

```
cargo build --release --manifest-path benchmarks/differential/rustref/Cargo.toml
python benchmarks/differential/run_rust_differential.py --root some/rust/project
```

The reference is a small helper crate that parses one file with `syn` and
prints the items and trait implementations it finds. `syn` is what procedural
macros are written against, so it is the reader the Rust ecosystem trusts.

First run across `ripgrep`, `serde` and `clap` — 648 files — found four
causes of invented implementations, all of them naming a type that does not
exist:

| Reported | Actual source |
|---|---|
| `std:From` | `impl From<E> for std::io::Error` — the first path segment, not the last |
| `a:Matcher` | `impl Matcher for &'a Foo` — the **lifetime** read as the owner |
| `mut:MapAccess`, `dyn:Display` | qualifiers read as type names |
| `where_clause:Args` | an `impl` inside a `quote!` body, which is a template |

Lifetimes are now a token kind of their own, so every consumer that filters on
identifiers ignores them without knowing they exist. Macro bodies are skipped,
since a real parser treats a macro invocation as one opaque item and never
descends into it.

| Crate | Files with invented impls, before | After |
|---|---:|---:|
| ripgrep | 6 | 2 |
| serde | 21 | 5 |
| clap | 19 | 0 |

**Two of those reductions came from fixing the reference, not the analyzer.**
`serde` declares visitor structs and their implementations inside function
bodies, and the helper walked only modules — so it reported real
implementations as absent and made the analyzer look like it was inventing
them. A reference is another program with its own defects, and disagreement
says only that one of the two is wrong.

What remains is a naming convention rather than a fabrication:
`impl Index<Match> for [u8]` is recorded against `u8` where `syn` says `[u8]`.
