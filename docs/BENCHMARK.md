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
