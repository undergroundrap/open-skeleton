# Product Requirements

## Product definition

`open-skeleton` is an independently designed, local-first codebase-intelligence and evidence-ledger platform. It helps humans and coding agents acquire verifiable context about unfamiliar software without requiring repository write access or default code execution.

## Required outcomes

### Deterministic understanding

- Inventory files, languages, roles, packages, and entry points.
- Extract symbols, imports, references, calls, routes, configuration reads, persistence operations, tests, and documentation assertions.
- Distinguish durable, process-local, external, generated, configured, and unknown state.
- Identify documentation-versus-code conflicts without silently choosing a convenient story.
- Track unsupported files, unresolved symbols, and analysis coverage.

### Evidence ledger

- Persist commit/content-pinned evidence receipts.
- Represent claims as `verified`, `inferred`, `conflict`, `unknown`, or `stale`.
- Preserve supporting and contradicting evidence.
- Record analyzer/provider versions and timestamps.
- Invalidate claims conservatively when dependent files or symbols change.

### Agent-native interfaces

- Human-readable and machine-readable CLI commands.
- A local MCP server with focused tools and accurate safety annotations.
- Portable JSON/JSONL schemas.
- Optional Codex, Claude, and local-model provider adapters.
- Bounded context packs that agents can query without reading the whole repository.

### Engineering quality

- Read-only target boundary by default.
- No target execution during static analysis.
- Tests, CI, packaging, threat model, deterministic fixtures, and benchmark gates.
- Explicit complexity and resource budgets.
- Progressive findings and stage-level timings.

### Comparative proof

- Analyze the same immutable `SINGLE-PLAYER-AI-MUD` commit used for the baseline exercise.
- Score `open-skeleton` and the exported commercial specification against the same human-adjudicated gold claims.
- Publish limitations and unsupported claims for both systems.
- Never claim broad superiority from a single fixture; state exactly which measured dimensions won or lost.

## Non-goals

- Copying another product's branding, interface, prompts, templates, or proprietary implementation.
- Recovering hidden prompts or private infrastructure.
- Treating citations as automatic proof.
- Autonomous source mutation in the initial product.
- Claiming equal analysis quality across every programming language.
- Optimizing for document length.

