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
