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
- `list_claims`
- `search_claims`
- `get_evidence`
- `list_symbols`
- `get_symbol_neighbors`
- `build_context_pack`
- `latest_diff`
- `refresh_analysis`

All query tools are annotated read-only and closed-world. `refresh_analysis` is idempotent for unchanged content, writes the configured ledger/exports, and reports `target_repository_write: false`.

The server constructor accepts no tool argument capable of changing its repository root. Start a separate process for a different repository. Keep the state directory private because evidence excerpts can contain source code.

The protocol contract test initializes an in-memory official SDK client, lists tools, performs a call, checks invalid input, and shuts down. It runs when the `mcp` extra is installed and is mandatory in CI.
