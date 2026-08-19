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

## Fact coverage, and the artifact you measure it against

Fact coverage asks a narrow question: of every fact the baseline asserts, how
many does a run of this engine also name? It is an upper bound -- a hit means
both documents name the same symbol, not that they say the same thing about it
-- and a baseline assertion that is simply wrong still counts against us.

The number depends entirely on which artifact is measured, and that is worth
stating plainly because measuring the wrong one understated this engine badly.

A run produces three files. `spec.md` is the readable specification. `spec.json`
is the structured projection. `spec.index.json` carries the name concordance --
every identifier a file binds or reaches for, deliberately exhaustive and
deliberately unranked, deliberately *not* in `spec.md`, because presenting a
loop variable beside a public function to a human buries the surface that
matters under the noise that does not.

Measured against `spec.md` alone, coverage of facts present in the repository
is 74.0%. Measured against everything the same run produced, it is 95.9%. That
22-point difference is not extraction; it is presentation. The report now says
which it measured, and only calls a shortfall "value the run did not produce"
when every artifact was passed.

| Repository | Present-in-repo facts | Carried | Coverage |
|---|---:|---:|---:|
| Reference web app (Next.js + FastAPI) | 4,244 | 4,068 | 95.9% |
| This repository | 4,297 | 4,096 | 95.3% |

Two repositories of different shape, language mix and size land within a point
of each other, which is the result that says something about the engine rather
than about either codebase.

The remaining category is separate and deliberately not chased. Of facts the
baseline asserts are *absent* from the repository -- technologies it checked
for and did not find -- a run carries 45.0%. Matching the rest means
reproducing a vendor checklist rather than reading a codebase, so the two rows
are reported separately and only the first is treated as a target.

```bash
python benchmarks/comparison/run_fact_coverage.py --baseline <tech_spec.md> --candidate <out>/spec.md <out>/spec.json <out>/spec.index.json --repo <repository> --output-dir <dir>
```

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

## Whole-document sweep across unseen repositories

```
python benchmarks/robustness/run_document_sweep.py --root .venv/Lib/site-packages
```

The robustness sweep above stops at analysis. This renders a specification for
each repository and asks the two questions that only matter once a document
exists: does it agree with itself, and does it account for everything the
ledger holds.

Both classes are invisible in any single repository, and each was found this
way rather than by reasoning. The first sweep turned up a crash instead of an
incoherence — one file in `sympy` is an arithmetic expression nested about
four hundred nodes deep, `ast.NodeVisitor` recurses per node, and the handler
covering parse did not cover the walk, so a 2,600-file repository produced no
analysis at all rather than a thin one.

The second class needed size rather than variety. A projection read one page
of claims and reported the page size as the ledger's contents; nothing smaller
than `java.base` had ever exceeded a page, so it sat behind every corpus until
one arrived holding 8,707 claims.

Exit status is non-zero when anything crashed or any document disagreed with
itself, which makes this usable as a gate over a corpus a team already trusts.

## Differential comparison against a real parser

```
npm install esbuild
python benchmarks/differential/run_differential.py --root some/typescript/project
python benchmarks/differential/run_java_differential.py --jdk-sources
```

The Java harness needs no setup at all: `--jdk-sources` reads `java.base` out
of the running JDK's own `lib/src.zip`, which is 3,064 files of real Java that
every installation already has. `javac -Xprint` runs the compiler front end
without generating code, so it works on a checkout that was never built.

It has one silent limit worth stating, because it decides what the harness can
and cannot certify. With an incomplete classpath `javac -Xprint` drops every
annotation from its output, reports the errors only on stderr, and exits zero.
Java puts its routes in annotations, so the declaration half is oracle-checked
and route extraction is fixture-tested — no compiler ever agreed with it.

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

### What the Rust differential now compares

Four families, added one at a time because each addition found something the
previous one could not see.

| Family | First run | After |
|---|---|---|
| Trait implementations | 46 files with invented owners | 2, both naming conventions |
| Declared items | 3 of 4 symbols fabricated on a probe | exact |
| Call sites | 104 of 110 files invented | 2 |
| Constants | 10 invented | **0 missed, 0 invented** |
| Struct fields | — | **748 on both sides, exact on the first run** |

The last two are the point of keeping a reference around. Constants needed
three fixes and the fields extractor needed none, and there was no way to know
which without asking a real parser.

**Roughly half of every disagreement has been the reference, not the analyzer.**
`syn` reports associated constants as `ImplItem::Const`, declares items inside
function bodies and closures, keeps the escape on a raw identifier, and counts
a tuple-struct construction as a call. Each looked exactly like a defect until
it was read. A differential says the two readers disagree; it never says which
one is wrong.

## Reading another generator's specification of this repository

The comparison that produced the most change was not a metric. It was putting
this engine's specification of its own repository beside another tool's, on
the same subject, and reading both.

Every automated check here measures whether a claim is *true*. None measures
whether the document is *readable*, and the tests are written by whoever wrote
the renderer, so they encode the same framing the renderer does. A document
aimed at the same reader does not.

Four subjects compared, three defects found, none of which was a false claim:

| Subject | What the comparison exposed |
|---|---|
| The SQLite ledger | Twelve sixty-four-character digests printed at a human reader under "Matched records" |
| Security architecture | Absence reported without attributing it to the decision that caused it |
| Non-goals | Six things the project refuses to build, reported as "stated obligations" |
| Integration surface | An absence stated as a failed probe rather than scoped to what was searched |

The third is worth recording plainly: it is the true-but-misleading shape
`open-skeleton audit` exists to catch, produced by this engine about itself, by
a claim added earlier in the same session as the audit that would have caught
it in someone else's output.

One idea from that document is not yet built. It reports absences by
foreclosure rather than by probe: instead of "no network client was found", it
lists all three `http`/`urllib` imports and observes that none of them is a
client. Doing that in general needs a candidate set per concern — the complete
list of things that could have satisfied it — which the profile does not
declare today. Naming the corpus an absence was measured against is the cheap
general form, and is what the `absent` verdict now states.
