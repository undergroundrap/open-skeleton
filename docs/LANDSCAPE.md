# Where Open Skeleton stands, and what it would take to lead

This is a positioning document. It keeps the rule the rest of this repository
keeps: a number is stated only with the command that reproduces it, a
comparison names what it does not cover, and a thesis is labelled a thesis.
External products are described from their own public documentation, and
their figures are reported as vendor-stated, not re-measured here.

## What this engine is trying to be

An agent that changes a repository needs to know three things about any
statement it relies on: what the statement is, what exact source supports or
contradicts it, and whether that source is still the source. Every tool below
answers the first. Very few answer the second in a form a machine can re-check,
and almost none answer the third.

Open Skeleton's mission is to make those answers data:

- a content-pinned **ledger of claims**, each carrying its snapshot, producer,
  status (`verified`, `inferred`, `conflict`, `unknown`, `stale`), confidence,
  and receipts on both sides;
- produced **deterministically**, with no target code executed, no network, no
  model in the path, and no write to the analysed repository;
- where **absence is a verdict** backed by the queries that found nothing, and
  a conflict stays a conflict until evidence resolves it;
- and where long documents, dashboards, MCP answers and model narratives are
  **projections** of that ledger, never an alternative source of truth.

## The field

Five families of tool overlap with that mission. Each is good at something this
engine is not.

| Family | Representative products | What it optimises | What it does not give an agent |
|---|---|---|---|
| Retrieval and context engines | Sourcegraph (SCIP, MCP server), Augment Context Engine, editor indexes in Cursor and Copilot | Finding the right code for a prompt, across very large or many repositories | A statement's status, or a receipt that can be re-verified later. A retrieved chunk is evidence for nothing until something reasons about it |
| Symbol tools over a language server | Serena | Compiler-accurate definitions, references and symbol-level edits | Claims about the program: what it serves, persists, refuses, or does not do |
| Code knowledge graphs over MCP | codebase-memory-mcp, CodeGraph, code-review-graph | Fast, persistent structure (symbols, calls, routes) so agents stop re-reading files | A distinction between a fact, an inference and a conflict; receipts pinned to content hashes; stale projection when the code moves |
| Generated documentation | DeepWiki, hours-long multi-agent specification generators | Readable narrative for humans, with links back to source | Determinism, and a way to check the narrative mechanically. DeepWiki's own documentation tells readers to verify load-bearing claims by hand |
| Precise static analysis | CodeQL, Semgrep, Kythe and Glean | Sound queries over a compiled or pattern-matched program | A repository-level account written for an agent, and `unknown` as a first-class answer rather than an empty result |

### Where Open Skeleton is ahead

These are properties, checked by tests in this repository, not adjectives.

- **Every citation is re-checkable.** `spec --verify` re-resolves every receipt
  against current source bytes and exits non-zero on any mismatch. On the
  pinned fixture, 860 citations are verified against content hashes; the
  registered external baseline carries 375 line references and verifies none
  (`benchmarks/comparison/run_comparison.py`).
- **The five-state claim model is enforced, not advisory.** A `verified` claim
  without supporting evidence cannot be constructed (`models.py`), and
  `unknown` and `conflict` survive every projection.
- **Staleness is computed.** Claims declare invalidation keys; a new snapshot
  projects older claims as stale rather than silently reusing them.
- **Negative space is reported with its proof.** An absent concern appears as
  an explicit verdict next to the probes that returned nothing.
- **It measures itself adversarially.** A canary repository built to fool
  extractors, differential checks against `javac -Xprint` across roughly
  18,000 JDK files with zero disagreements, reader-parity checks across
  languages, and an audit that looks for the shape of past mistakes.
- **An agent loop can gate on it.** `scripts/turn_gate.py` separates "the work
  is wrong" (exit 1) from "the gate could not run" (exit 2), so a busy ledger
  never reads as a rejected change.
- **The result does not depend on how it was computed.** Serial and parallel
  runs are byte-identical in the ledger and exports on every repository
  measured (`benchmarks/scaling/run_corpus.py` fails otherwise).

On the one fixture with an independently enumerated gold set, this engine
recovers 34 of 34 material claims with correct evidence in seconds, against
89.7% recall, 95.6% evidence correctness and 75.0% conflict detection for the
registered external specification, which took about five hours and
forty-seven minutes. That is one author-reviewed fixture. It is evidence that
the approach works, not proof of general superiority, and the README says so.

### Where it is behind

A positioning document that listed only strengths would be the kind of
artifact this engine exists to replace.

- **Language depth.** TypeScript, Rust and Java are read lexically. A language
  server or SCIP index resolves types, overloads and cross-file references this
  engine cannot. The Hum adapter already shows the right pattern: ingest a
  compiler-produced index as a receipted source rather than reimplement the
  compiler.
- **Raw indexing throughput.** Graph-only indexers report whole-repository
  indexing in milliseconds for average repositories and minutes for the Linux
  kernel (vendor-stated). This engine takes 37 seconds end to end on Django
  with four workers, because it writes about 270,000 hash-pinned receipts and
  the claims built on them. The receipts are the product, but the gap is real.
- **Incremental re-analysis.** A one-line edit re-reads the whole repository.
  Every identifier embeds the snapshot id, so nothing computed for one snapshot
  can be reused for the next; see the roadmap below.
- **Scope of the baseline comparison.** The external specification carries a
  requirements catalogue, interface analysis and operational material this
  engine does not attempt. Fact coverage is 96.9% of what that document says is
  present in the repository and 43.3% of what it says is absent.
- **No semantic search.** Queries are over claims, symbols and FTS5 text, not
  embeddings. For "find code that does something like this", retrieval engines
  are better tools, and should be used beside this one.

## Performance: what changed, and how it was proven

The largest repository measured before this work, Django (5,698 files, 1.1
million lines), took 101 seconds end to end. It now takes 65 seconds serially
and 37 seconds with four workers, with byte-identical output. Details, per
stage and per repository, are in [PERFORMANCE.md](PERFORMANCE.md). The changes,
in order of effect:

1. **A quadratic cross-reader pass removed.** The orphan-module census compared
   every Python module with every import edge: 18 seconds on Django, now 78 ms.
2. **Readers share their files across processes.** Python, TypeScript and Rust
   files are split into consecutive, byte-balanced chunks; results merge in file
   order, so the output cannot depend on the worker count.
3. **A faster AST traversal with the same order.** `FastNodeVisitor` and `walk`
   are held to node-for-node identity with the standard library over this
   repository's own sources.
4. **A ledger pathology fixed.** Re-saving an unchanged analysis took 404
   seconds on clap, against one second for the first save, because `INSERT OR
   REPLACE` fired an unindexed foreign-key cascade per row. The same cascade
   stripped receipts from earlier claims that cited the same evidence. Upserts
   fix both; a regression test fails on the old code.
5. **Exports without deep copies.** JSONL export no longer runs `asdict` per
   record, and reuses one encoder.

The proof is the same for each: fingerprints of the ledger and the exports,
with the clock pinned, are compared before and after and across worker counts.
The pinned benchmark still scores 100% recall, precision, evidence correctness
and conflict detection.

## Thesis: what this could change about agentic engineering

What follows is argument, not measurement.

**Agents need a memory that can be wrong out loud.** Today an agent's
understanding of a codebase is a context window: unversioned, unverifiable and
silently stale after the next edit. A ledger whose entries carry receipts and
invalidation keys turns that into something an agent can check before acting
on, and something that says `stale` instead of being quietly out of date.

**Verification becomes cheaper than generation.** A model can draft any
narrative about a repository; checking it has required a human. When every
material statement cites a content-hashed receipt, checking a narrative is a
join, and a model's output can be rejected mechanically when it cites nothing
the ledger holds. `synthesize` and `assemble-synthesis` already enforce this:
claim IDs outside the supplied context pack are refused.

**Hand-offs between agents become contracts.** A planner, an implementer and a
reviewer that share a ledger can disagree about a claim's status, but not about
what the evidence was. Conflicts are preserved rather than averaged away, which
is what multi-agent systems most often lose.

**The gate belongs in the loop, not in the prompt.** A 49,000-token
specification read every turn stops being read. A gate that runs between turns,
prints nothing when everything holds and returns a meaningful exit code when it
does not, costs an agent nothing until it matters. That is why the analysis now
parallelises by default on the command line: the gate's latency is the loop's
latency.

If those four hold, the unit of trust in agentic engineering moves from "the
model seemed confident" to "the receipt still verifies". That is the change
this engine is built to make.

## What it would take to lead

Ordered by what most limits the mission today.

1. **Snapshot-independent fact identity.** Key per-file facts by file content
   hash and reader version, and bind them to a snapshot in a separate relation.
   That single change makes incremental re-analysis correct by construction:
   an unchanged file's facts are reused, not recomputed. The per-file outcome
   types introduced for parallel reading are the unit that would be cached.
2. **Ingest compiler-accurate indexes.** Accept SCIP (and language-server
   output) as receipted evidence, the way the Hum semantic graph is ingested,
   so TypeScript, Rust and Java facts can be `verified` against a resolver
   rather than a lexer.
3. **A ledger schema for scale.** Integer surrogate keys or `WITHOUT ROWID`
   tables, behind a backward-compatible migration, would cut the write path,
   now the largest serial stage on Django.
4. **Gold sets on unseen repositories.** One author-reviewed fixture is a
   start. Independent adjudication on repositories nobody tuned against is the
   only result that would support a general claim, and the benchmark harness is
   already built to accept it.
5. **A claim-check MCP tool.** "Is this claim still true at HEAD?" as a single
   read-only call, so any agent -- including ones built on the retrieval
   engines above -- can use this ledger as its verifier.

## Sources

- [Sourcegraph MCP server](https://sourcegraph.com/mcp) and
  [context engineering guide](https://sourcegraph.com/blog/context-engineering)
- [Augment Context Engine MCP](https://docs.augmentcode.com/context-services/mcp/overview)
- [Serena](https://github.com/oraios/serena)
- [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) and
  [CodeGraph](https://github.com/codegraph-ai/CodeGraph)
- [DeepWiki documentation](https://docs.devin.ai/work-with-devin/deepwiki)
- [Blitzy: how it works](https://blitzy.com/how_it_works), as an example of
  hours-long multi-agent generation; it is not identified as either registered
  baseline, which this repository deliberately leaves unnamed
- [CodeWiki: evaluating holistic documentation for large codebases](https://arxiv.org/pdf/2510.24428)
