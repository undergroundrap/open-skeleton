# Open Skeleton

[![CI](https://github.com/undergroundrap/open-skeleton/actions/workflows/ci.yml/badge.svg)](https://github.com/undergroundrap/open-skeleton/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

Open Skeleton is a local-first, evidence-first codebase-intelligence engine for developers and agents. It turns an untrusted repository into a content-pinned ledger of symbols, relationships, claims, conflicts, coverage, and source receipts before any model is allowed to write a narrative.

It is deliberately not a “paste the whole repository into an LLM” wrapper. The deterministic path has no required third-party runtime dependencies, performs no target-code execution or network access, and does not modify the analyzed repository.

## Why it exists

Large specifications are useful only when engineers can answer three questions quickly:

1. What is the claim?
2. What exact source evidence supports or contradicts it?
3. Is that evidence still current for this snapshot?

Open Skeleton stores those answers as queryable data. Concise Markdown, a local dashboard, MCP tools, and optional model summaries are projections of the ledger—not alternate sources of truth.

## Implemented capabilities

- Bounded traversal with symlink, secret, binary, generated-output, malformed-encoding, and file-size controls
- Deterministic SHA-256 file and snapshot identities
- Python AST symbols, imports, calls, FastAPI routes, typed parameters, state mutation, SQLite/JSON persistence, CORS, tests, process exits, and selected failure behavior
- Comment-safe Rust lexical facts for `unsafe` surface, panicking call sites, `#[test]` census, items, imports, call edges, served routes in builder and attribute-macro form, outbound client requests, `const`/`static` tunables, struct fields, impl methods, shared statics, the Result/`?` error surface, and trait implementations
- Comment-safe TypeScript/JavaScript lexical facts for value bindings, class and interface members, object literal keys, imports, served routes, the endpoint each call targets, named literal tunables including inside an IIFE wrapper, endpoint literals, React hooks resolved through aliased imports, browser storage, module-scope state, `process.env` reads, thrown types, and tests
- Comment-safe Java lexical facts for declared types and their kinds, the supertypes each one names, public surface including an enum's constants and a record's components, annotation-declared HTTP routes, program entry points, non-final static state, and annotated tests. A type declared inside a method body is marked local rather than published, because nothing can name it
- Declared commitments: the obligations a repository wrote down for itself in a requirements document, threat model, contributing guide, or architecture decision record. Recording that a promise was made is a different fact from whether the code keeps it, and the claim says which one it is
- The error contract a package publishes: the exception types it declares, and the family every `except` clause absorbs — where the author decided a fault was survivable
- Source excerpts printed beneath the claims they back, and withheld when the file's hash no longer matches the receipt, when the span is a whole file rather than a place, or when the path resolves outside the analyzed root
- Library-shaped facts an application taxonomy has no category for: the public surface a module commits to through `__all__`, and the paths it has scheduled for removal through deprecation warnings
- Project-manifest and Markdown reconciliation for dependencies, API tables, runtime instructions, Tailwind claims, tests, and CI
- Native `hum.semantic_graph.v0` ingestion without implicitly executing the Hum compiler
- Atomic `verified`, `inferred`, `conflict`, `unknown`, and `stale` claims with alternatives and invalidation keys
- Snapshot diffs and stale-claim projection
- SQLite ledger, JSONL export, concise source-linked Markdown, and a loopback-only dashboard
- Outline-driven long-form specifications from user-editable profiles, where absence is a verdict backed by the query that found nothing
- Per-route sequence diagrams and a persistence entity diagram, generated from the graph rather than narrated
- Implemented-capability catalog with traceability computed from call edges and route-path literals, naming the capabilities no test or harness reaches
- Declared-surface extraction a symbol index misses: model fields, function signatures with defaults as written, returned payload shapes, object literal keys, and imported names per module
- Runtime-reach extraction: platform API called through imports, and third-party hosts named in stylesheets and markup that no dependency manifest shows
- Substitute analysis, so an absent concern names the structure doing its job instead of stopping at the absence
- A security control matrix, an endpoint catalog with per-handler guards and refusals, and module-level data flow
- Per-section provenance: which files each section's conclusions were read out of
- Citation integrity verification that re-resolves every receipt against current source bytes
- Self-consistency checks that read the generated document and fail it when the prose disagrees with the data it was projected from
- Java declarations and supertypes verified against `javac -Xprint` across eleven JDK modules — roughly 18,000 files including `java.base`, `java.desktop` and `jdk.compiler` — with zero disagreements in either
- Read/query/analyze MCP service using the official Python SDK as an optional extra
- Explicit, disableable Codex CLI, Claude Code, and local-command synthesis adapters behind one JSON contract
- Machine-readable, pinned comparative benchmarks with source-receipt validation

## Quick start

Open Skeleton is not published to PyPI yet, so install it from source.
[uv](https://docs.astral.sh/uv/) is the shortest path because it fetches a
matching interpreter itself:

```powershell
uv tool install git+https://github.com/undergroundrap/open-skeleton
open-skeleton analyze C:\path\to\repository
```

Once a release is published, `uv tool install open-skeleton` will work without
the URL.

Otherwise Python 3.12 or newer is required. The deterministic path has no
third-party runtime dependencies, so there is nothing else to resolve.

```powershell
python -m pip install -e .
open-skeleton analyze C:\path\to\repository
open-skeleton claims C:\path\to\repository --status conflict
open-skeleton search "process-local" C:\path\to\repository
open-skeleton serve C:\path\to\repository
```

The dashboard is available at `http://127.0.0.1:8765` and accepts loopback hosts only.

Without installation:

```powershell
$env:PYTHONPATH = "src"
python -m open_skeleton analyze C:\path\to\repository
```

By default, Open Skeleton writes to a deterministic per-repository directory under the OS-local state area, never inside the target repository. On Windows this begins at `%LOCALAPPDATA%\open-skeleton\state`; on Linux it uses `$XDG_STATE_HOME/open-skeleton/state` (or `~/.local/state`), and on macOS it uses `~/Library/Application Support/open-skeleton/state`. Use `--state-dir` for an explicit external location; paths inside the analyzed repository are rejected.

```text
<local-state>/open-skeleton/state/<repository>-<path-hash>/
|-- evidence.sqlite3
|-- inventory.jsonl
|-- inventory.md
|-- analysis.jsonl
`-- analysis.md
```

## Command surface

```text
open-skeleton scan        bounded inventory only
open-skeleton analyze     inventory plus deterministic semantic analysis
open-skeleton status      latest snapshot and analysis status
open-skeleton claims      filter atomic claims
open-skeleton search      search the claim ledger
open-skeleton evidence    inspect one immutable receipt
open-skeleton diff        compare snapshots and project stale claims
open-skeleton serve       run the read-only local dashboard
open-skeleton synthesize  explicitly invoke an optional provider
open-skeleton plan-synthesis  build independent source-grounded narrative jobs
open-skeleton run-synthesis-plan  dry-run or execute bounded narrative jobs
open-skeleton assemble-synthesis  validate receipts and render narrative Markdown
open-skeleton benchmark   score a pinned fixture and baseline artifact
open-skeleton spec        render a long-form specification from an outline profile
open-skeleton audit       flag claim groups shaped like a known mistake
```

## Long-form specifications

```powershell
open-skeleton spec C:\path\to\repository --verify
open-skeleton spec C:\path\to\repository --profile my-team-checklist.json
```

The outline is data, not a prompt. Each node declares re-runnable probes, so a
concern the repository does not implement is reported as an explicit `absent`
verdict next to the exact queries that returned nothing — not omitted. `--verify`
re-resolves every citation against current source bytes and exits non-zero if any
receipt no longer holds. See [docs/SPEC.md](docs/SPEC.md).

## Auditing the analysis itself

Every wrong answer this engine has produced was a true statement in the wrong
frame — routes registered inside a test suite reported as the served surface,
a constant lookup table reported as a queue, callback parameters reported as
platform API. Each analyzer was correct in isolation, so no unit test caught
any of them.

```powershell
open-skeleton audit C:\path\to\repository
```

This looks for the *shape* of that mistake rather than for particular
instances: a production finding evidenced only by test files, a category with
no file-level evidence at all, a category concentrated in a file that carries
little else. A finding is a place to read before publishing a number, not a
defect — `--strict` exits non-zero if you want it as a gate.

## Gating an agent loop

An agent that rewrites a repository can consult the evidence between turns.
The temptation is to put the specification in the prompt; it is roughly
49,000 tokens, and a document read every turn stops being read at all.

```powershell
python scripts\turn_gate.py --repo C:\path\to\repository --fast
```

The gate analyzes, audits, and — without `--fast` — renders the document and
checks it against itself and against the ledger. It prints nothing when
everything passes. The exit code is the interface:

| Code | Meaning | What a loop should do |
|---|---|---|
| `0` | Every gate passed | Proceed |
| `1` | A gate found something, reason on stdout | Surface it |
| `2` | The gate could not run | Retry — **not** a verdict on the work |

That third code exists because the distinction is easy to lose and expensive
to lose. An agent wired to treat any non-zero status as rejection will reject
good work whenever the ledger is busy, for a reason no author can act on. A
gate is only as useful as its false-positive rate: the first version of this
one fired on every git repository ever analyzed, which would have rejected
every change forever until somebody turned the gate off.

Two guards protect against a green result that means nothing:

```powershell
python scripts\turn_gate.py --repo . --require-language Hum --min-coverage 0.95 --min-yield 0.1
```

`--require-language` fails when a language the scanner found is read below
`--min-coverage`. `--min-yield` fails when files are read and almost none
produces a claim. They answer different questions — coverage is whether the
analyzer reached the code, yield is whether it understood any of it — and
conflating them lets a repository be certified by a tool that read every byte
and concluded nothing. `--min-yield` defaults to `0` deliberately: reading a
file and having nothing to say about it is a legitimate answer, and a gate
that demands findings teaches an analyzer to invent them.

## Precision under hostile input

Every fixture used to build these analyzers was written by someone trying to
make a working program, and real code is cooperative. An extractor only looks
precise until it meets a decorator that is commented out, aliased, or applied
in a loop, so `tests/test_canary_repository.py` is a repository written
specifically to fool one.

| Case | What it separates | Result |
|---|---|---|
| A route inside a comment | Regex scanning from syntax parsing | Not extracted |
| A route inside a string literal | The same, through the other common leak | Not extracted |
| An aliased decorator or import | Name resolution from literal matching | Resolved |
| A path built in a loop or template | Static extraction from execution | Not invented |
| A receiver of unknown origin | Whether a guess is preferred to silence | No claim either way |
| A genuine route, call, and hook | That the above is precision, not blindness | Extracted |

The standard is precision rather than recall. An analyzer honestly reported as
lexical is allowed to miss a dynamically registered route; it is not allowed
to assert one that does not exist, because a specification naming an endpoint
nobody serves is worse than one omitting an endpoint somebody does. A
template path is recorded as its static prefix marked interpolated —
`/api/user/` rather than a fabricated `/api/user/42`.

The last row exists because the first version of this fixture passed in every
language and the result was misleading: two of three analyzers passed each
trap by extracting nothing at all. A test suite that only checks for false
positives rewards blindness.

## Agent access with MCP

Install the optional SDK and run a repository-bound stdio server:

```powershell
python -m pip install -e ".[mcp]"
open-skeleton-mcp C:\path\to\repository --state-dir C:\safe\state\directory
```

The server exposes status, coverage, claims, evidence, symbols, relationships, context packs, diffs, and an explicit refresh tool. Query tools are annotated read-only. Refresh writes the configured ledger and exports, never the target repository. See [docs/MCP.md](docs/MCP.md).

## Optional synthesis providers

Deterministic analysis works with providers disabled. `synthesize` sends only a bounded context pack, validates a strict result schema, and rejects claim IDs not present in that pack.

```powershell
open-skeleton synthesize "state ownership" C:\repo --provider disabled
open-skeleton synthesize "state ownership" C:\repo --provider codex
open-skeleton synthesize "state ownership" C:\repo --provider claude
open-skeleton synthesize "state ownership" C:\repo --provider local-command --command my-local-adapter
```

For a whole document, `plan-synthesis` first creates one independent bounded job
per non-structural outline obligation without contacting a model. The companion
runner is also a dry run unless `--execute` is present; it supports bounded
concurrency, exact-request resume, and atomic external result receipts while the
local ledger remains the source of truth.

```powershell
open-skeleton plan-synthesis C:\repo
open-skeleton run-synthesis-plan C:\repo --provider codex
open-skeleton run-synthesis-plan C:\repo --provider codex --model MODEL_ID --execute --concurrency 4
open-skeleton assemble-synthesis C:\repo --results-dir C:\private\synthesis-runs\codex-cli
```

Provider invocation is explicit and may incur third-party cost or network
activity. See [source-grounded synthesis](docs/SYNTHESIS.md) and
[provider adapters](docs/PROVIDERS.md).

### Full-document parity proof

The comparison suite can freeze every nonblank line in a registered baseline and
candidate, create blinded Claude/Codex review batches, reconcile their independent
proposals, and generate a human adjudication file. It never calls a provider without
`--execute`, and it never treats model agreement as proof. `parity_proven` can become
true only after a named human verifies every block's semantic atoms; removing an
incorrect baseline item from the denominator requires a second human and repository
evidence. Private baseline-derived files are rejected inside every Git worktree.

This proves semantic coverage only for the exact hash-pinned artifact and fixture. It
does not prove universal quality on unseen repositories. See the
[strict parity protocol](docs/SYNTHESIS.md#proving-whether-the-conclusions-are-truly-present).

## Reproducible AI-MUD benchmark

The included gold set pins `SINGLE-PLAYER-AI-MUD` commit `93ebd51cb4083d2307564c265394358e53c4f5ca` and 34 material claims. On the August 30, 2026 local run:

| System | Material recall | Scoped precision | Evidence correctness | Conflict detection |
|---|---:|---:|---:|---:|
| Open Skeleton | 100.0% | 100.0% | 100.0% | 100.0% |
| Registered external baseline | 89.7% | 89.7% | 95.6% | 75.0% |

Open Skeleton completed end to end in about 3.8 seconds; the supplied baseline artifact took approximately 5 hours 47 minutes. That is 0.0183% of the recorded external wall time, a baseline/candidate elapsed-time ratio of about 5,477x. The generated specification is 90 sections and roughly 50,800 words, against approximately 180,800 words for the baseline. The external timing is an author-recorded historical observation, not a same-machine rerun.

**These numbers describe one author-reviewed fixture, not universal product superiority**, and the comparison is deliberately narrow. It measures whether the material findings of a long-form specification can be reproduced deterministically and cited verifiably. It does not measure breadth: the baseline artifact also contains a requirements catalog, process and state-machine diagrams, architectural decision records, and user-interface analysis that Open Skeleton does not attempt. Baseline precision is limited to statements mapped to the material gold set, and peak memory is Python allocation data rather than process RSS. See [docs/BENCHMARK.md](docs/BENCHMARK.md).

## Head-to-head comparison

`benchmarks/comparison/run_comparison.py` measures this engine against one of the
two registered external specifications. It verifies the private export's public
SHA-256 receipt, repository revision, and clean fixture before generating our
candidate. The baseline artifacts are not redistributed here, so supply the
matching export to reproduce a comparison.

```powershell
python benchmarks\comparison\run_comparison.py `
  --repository C:\path\to\fixture `
  --baseline C:\path\to\baseline\tech_spec.md `
  --baseline-id external-single-player-ai-mud-2026-08-04 `
  --output-dir comparison-output
```

On `SINGLE-PLAYER-AI-MUD`, against the registered external export of the same
author-recorded commit:

| Measure | Open Skeleton | Baseline |
|---|---:|---:|
| Generation time | ~3.8 s | 5 h 47 m |
| Diagrams | 83 | 82 |
| References carrying a line number | 549 | 375 |
| Citations verified against source hashes | 860 | 0 |
| Citation integrity | 100% | not reported |

The two documents do not attempt the same scope: the baseline carries a
requirements catalog and interface analysis this engine does not produce, and it
is roughly three times longer. The rows that matter are the last two.
A reference naming only a file cannot be checked; a citation pinned to a content
hash is re-resolved on every `spec --verify` run.

### How much of the baseline's content is carried

Counting diagrams and citations describes shape, not content. This asks the
harder question directly: enumerate every fact the baseline asserts, then check
whether this engine's output carries it.

```powershell
python benchmarks\comparison\run_fact_coverage.py `
  --baseline C:\path\to\baseline\tech_spec.md `
  --baseline-id external-single-player-ai-mud-2026-08-04 `
  --candidate spec-output\spec.md spec-output\spec.json spec-output\spec.index.json `
  --repo C:\path\to\fixture `
  --output-dir coverage-output
```

| Fact origin | Baseline asserts | Open Skeleton carries | Coverage |
|---|---:|---:|---:|
| Present in the repository | 4,192 | 4,064 | 96.9% |
| Asserted absent from it | 630 | 273 | 43.3% |
| **All facts asserted** | **4,822** | **4,337** | **89.9%** |

A baseline names two different kinds of thing. Some are facts about the code:
a symbol, a path, a value that exists. Others are technologies it checked for
and did not find, named to record their absence — matching those means
reproducing somebody's vendor checklist, and a repository running none of those
services cannot contain them however good the extraction is. `--repo` splits the
two by testing each fact against the sources, so the split is reproducible
rather than asserted. Both rows are reported because dropping the second would
be moving the goalposts.

Whatever is missing is listed by name in the report. That list, not the
percentage, is the useful output.

## Hum language support

Open Skeleton consumes a versioned semantic graph produced by Hum tooling. Generate
one covering every Hum file — `hum graph` accepts multiple paths and merges them into
a single index — then supply it:

```powershell
hum graph (Get-ChildItem -Recurse -Filter *.hum | ForEach-Object FullName) > graph.json
open-skeleton analyze C:\path\to\repository --hum-index graph.json
```

Repeat `--hum-index` to combine sharded indexes; each keeps its own hashed receipt, and
a file covered by more than one index is analyzed once. An index that omits files is
reported as partial coverage rather than treated as complete. If a `.hum` repository has no supplied native index, Open Skeleton reports exact zero semantic coverage and explicitly states that it did not execute the compiler. This creates a safe path for using the engine to improve Hum programs—and eventually to inspect Hum’s own architecture—without pretending a generic parser understands the language.

## Development

```powershell
python -m compileall -q src tests benchmarks
python -m unittest discover -s tests -v
python benchmarks\scaling\run_scaling.py
python -m pip wheel . --no-deps --no-build-isolation --no-cache-dir --wheel-dir dist
```

CI runs tests on Windows and Linux, the optional MCP protocol contract, Ruff, Mypy, dependency audit, distribution build, and the pinned public benchmark.

## Trust and scope

- Default analysis does not execute repository code, install its dependencies, or contact the network.
- Static evidence does not prove runtime behavior.
- Inferences retain alternatives; conflicts remain unresolved until a human or stronger evidence resolves them.
- The local-command provider is arbitrary code execution by explicit user choice and runs only from the provider workspace.
- Automatic source edits, commits, pushes, and pull requests are intentionally out of scope.

## Adding a language

An analyzer emits symbols, relationships, evidence, claims and one coverage
record. Everything downstream is a projection of those five types, so nothing
else needs to know the language exists. The Rust adapter is about 400 lines.
See [docs/ADDING_AN_ANALYZER.md](docs/ADDING_AN_ANALYZER.md).

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md), and [SECURITY.md](SECURITY.md) before extending trust boundaries.

## Independent development

Open Skeleton is independently designed from public documentation, standard compiler/indexing/database techniques, direct inspection of repositories Ocean Bennett owns or is authorized to analyze, and supplied product output. It contains no third-party vendor source code, private API, hidden prompt, or copied proprietary expression. See [docs/PROVENANCE.md](docs/PROVENANCE.md).

## License

Copyright (c) 2026 Ocean Bennett.

Licensed under AGPL-3.0 with the visible attribution term in [NOTICE.md](NOTICE.md). See [LICENSE](LICENSE) for the full license.
