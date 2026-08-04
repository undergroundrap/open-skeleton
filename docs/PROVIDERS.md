# Optional provider adapters

Providers synthesize bounded ledger context; they do not analyze the repository directly.

## Shared contract

Input contains task, snapshot ID, bounded context pack, output schema, optional model, and timeout. Output contains summary, findings with claim IDs and caveats, conflicts, and unknowns.

Open Skeleton rejects:

- invalid JSON
- missing or extra top-level fields
- malformed finding objects
- non-string conflict/unknown entries
- claim IDs absent from the supplied context pack

Every request is SHA-256 identified and every result is persisted beneath the state directory.

## Adapters

- `disabled`: deterministic-only mode
- `codex`: ephemeral Codex CLI execution with a read-only sandbox and output schema
- `claude`: Claude Code print mode with plan permissions and read/write/shell/web tools denied
- `local-command`: JSON stdin/stdout protocol for an explicitly selected local executable

Codex and Claude may use network access and paid accounts. Open Skeleton cannot promise that a third-party provider is free. The local-command adapter can execute arbitrary code; use only commands you trust and keep its workspace separate from the target repository.
