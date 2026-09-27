# MCP integration

Install the optional official SDK:

```powershell
python -m pip install -e ".[mcp]"
```

Run the repository-bound stdio server:

```powershell
open-skeleton-mcp C:\path\to\repo --state-dir C:\path\to\state
```

## Tools

- `project_status`
- `analysis_coverage`
- `list_contracts` — contracts declared in more than one form, with a path and line for every site
- `list_claims`
- `search_claims`
- `get_evidence`
- `check_claims` — whether each claim still rests on the current source, without re-analysing
- `list_symbols`
- `get_symbol_neighbors`
- `build_context_pack`
- `latest_diff`
- `refresh_analysis`

All query tools are annotated read-only and closed-world. `refresh_analysis` is idempotent for unchanged content, writes the configured ledger/exports, and reports `target_repository_write: false`.

`check_claims` rescans the repository read-only and compares it with the
snapshot the claims were read from. Each claim comes back `current`, `stale`
with its reasons and the receipts whose files changed or disappeared, or
`not-found`. Invalidation keys that no file inventory can evaluate, such as
`git:HEAD`, are listed under `unevaluated_keys` rather than assumed to hold. It
writes no snapshot and no ledger row, so an agent can call it between edits to
learn which of the facts it is relying on it must re-read, at the cost of a scan
rather than an analysis.

`refresh_analysis` uses one worker process per core, up to eight, above 1.5 MB
of source; `open-skeleton-mcp --jobs 1` keeps it serial.

The server constructor accepts no tool argument capable of changing its repository root. Start a separate process for a different repository. Keep the state directory private because evidence excerpts can contain source code.

The protocol contract test initializes an in-memory official SDK client, lists tools, performs a call, checks invalid input, and shuts down. It runs when the `mcp` extra is installed and is mandatory in CI.
