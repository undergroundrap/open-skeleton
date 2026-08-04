# Open Skeleton

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
- Comment-safe TypeScript/JavaScript lexical facts for symbols, imports, fetch calls, endpoint literals, React hooks, browser storage, and tests
- Project-manifest and Markdown reconciliation for dependencies, API tables, runtime instructions, Tailwind claims, tests, and CI
- Native `hum.semantic_graph.v0` ingestion without implicitly executing the Hum compiler
- Atomic `verified`, `inferred`, `conflict`, `unknown`, and `stale` claims with alternatives and invalidation keys
- Snapshot diffs and stale-claim projection
- SQLite ledger, JSONL export, concise source-linked Markdown, and a loopback-only dashboard
- Outline-driven long-form specifications from user-editable profiles, where absence is a verdict backed by the query that found nothing
- Implemented-capability catalog with traceability computed from call edges and route-path literals, naming the capabilities no test or harness reaches
- Citation integrity verification that re-resolves every receipt against current source bytes
- Read/query/analyze MCP service using the official Python SDK as an optional extra
- Explicit, disableable Codex CLI, Claude Code, and local-command synthesis adapters behind one JSON contract
- Machine-readable, pinned comparative benchmarks with source-receipt validation

## Quick start

Python 3.12 or newer is required.

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
open-skeleton benchmark   score a pinned fixture and baseline artifact
open-skeleton spec        render a long-form specification from an outline profile
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

Provider invocation is explicit and may incur third-party cost or network activity. See [docs/PROVIDERS.md](docs/PROVIDERS.md).

## Reproducible AI-MUD benchmark

The included gold set pins `SINGLE-PLAYER-AI-MUD` commit `93ebd51cb4083d2307564c265394358e53c4f5ca` and 33 material claims. On the August 4, 2026 local run:

| System | Material recall | Scoped precision | Evidence correctness | Conflict detection |
|---|---:|---:|---:|---:|
| Open Skeleton | 100.0% | 100.0% | 100.0% | 100.0% |
| Supplied commercial baseline | 89.4% | 89.4% | 95.5% | 75.0% |

The release-candidate run reached its first finding in 691 ms and completed in 1.247 seconds; the supplied baseline artifact took approximately 5 hours 47 minutes. Open Skeleton's concise report was about 4,100 words versus approximately 180,800 words for the baseline.

These numbers describe one author-reviewed fixture, not universal product superiority. Baseline precision is limited to statements mapped to the material gold set, and peak memory is Python allocation data rather than process RSS. See [docs/BENCHMARK.md](docs/BENCHMARK.md).

## Hum language support

Open Skeleton consumes a versioned semantic graph produced by Hum tooling. If a `.hum` repository has no supplied native index, Open Skeleton reports exact zero semantic coverage and explicitly states that it did not execute the compiler. This creates a safe path for using the engine to improve Hum programs—and eventually to inspect Hum’s own architecture—without pretending a generic parser understands the language.

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

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md), and [SECURITY.md](SECURITY.md) before extending trust boundaries.

## Independent development

Open Skeleton is independently designed from public documentation, standard compiler/indexing/database techniques, direct inspection of repositories Ocean Bennett owns or is authorized to analyze, and supplied product output. It contains no third-party vendor source code, private API, hidden prompt, or copied proprietary expression. See [docs/PROVENANCE.md](docs/PROVENANCE.md).

## License

Copyright (c) 2026 Ocean Bennett.

Licensed under AGPL-3.0 with the visible attribution term in [NOTICE.md](NOTICE.md). See [LICENSE](LICENSE) for the full license.
