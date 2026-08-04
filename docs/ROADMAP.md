# Roadmap

## Completed foundation

- bounded inventory, content hashes, SQLite ledger, JSONL, and concise Markdown
- Python AST, JavaScript/TypeScript lexical, project metadata, and Hum semantic-index adapters
- evidence receipts, five claim states, conflicts, alternatives, diffs, and stale projection
- CLI, repository-bound MCP service, loopback dashboard, and optional provider adapters
- pinned AI-MUD benchmark, performance smoke tests, Windows/Linux CI, and package build

## Next language depth

- TypeScript Compiler API worker for resolved types, JSX ownership, and cross-file symbols
- tree-sitter fallback for more languages with explicit coverage labels
- native Hum compiler export integration and Hum self-analysis benchmark
- database migration/schema adapters beyond inline SQLite DDL

## Next analysis depth

- framework rule packs versioned separately from parsers
- call-graph resolution, endpoint-to-client contract comparison, and data-flow taint summaries
- fine-grained invalidation and bounded per-symbol recomputation
- runtime verification as a separately permissioned sandbox profile
- full-output external adjudication across multiple diverse public fixtures

## Product direction

- saved views and issue triage in the local dashboard
- incremental file-watch mode with debounce and cancellation
- signed portable evidence bundles
- IDE and CI annotations
- optional specialist/challenger orchestration that can never bypass receipt requirements

Automatic repository mutation remains out of scope until it has a separate capability profile, threat model, and review gate.
