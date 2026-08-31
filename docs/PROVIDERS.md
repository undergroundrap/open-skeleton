# Optional provider adapters

Providers synthesize bounded ledger context; they do not analyze the repository directly.

For whole-document work, `open-skeleton plan-synthesis` builds one bounded,
parallel-safe job per non-structural outline obligation without invoking any
provider. `open-skeleton run-synthesis-plan` is also model-free by default and
requires `--execute` before it dispatches the validated jobs. It supports bounded
concurrency, exact-request resume, and atomic source-derived results outside Git. See
[source-grounded synthesis](SYNTHESIS.md).

`open-skeleton assemble-synthesis` is a separate model-free step. It requires one
complete, exact-plan receipt per job and verifies all cited claim IDs before producing
readable Markdown. The generated narrative never replaces the deterministic
specification or evidence ledger.

## Shared contract

Input contains task, snapshot ID, bounded context pack, output schema, optional model, and timeout. Output contains summary, findings with claim IDs and caveats, conflicts, and unknowns.

Open Skeleton rejects:

- invalid JSON
- missing or extra top-level fields
- malformed finding objects
- non-string conflict/unknown entries
- claim IDs absent from the supplied context pack

Reasoning-review batches use a separate strict contract. It requires exactly one
proposal for every requested unit and rejects invented evidence or candidate-block
IDs, incomplete coverage, incompatible materiality/status combinations, and any
attempt to treat a baseline-invalid decision as final. Review results never become
ledger facts automatically.

Every request is SHA-256 identified and every result is persisted beneath the state directory.

## Adapters

- `disabled`: deterministic-only mode
- `codex`: ephemeral Codex CLI execution with a read-only sandbox, no repository
  requirement, user configuration and rules ignored, color disabled, and an output
  schema
- `claude`: Claude Code structured print mode with restricted and safe modes, plan
  permissions, no saved session, one turn, and all tools denied
- `local-command`: JSON stdin/stdout protocol for an explicitly selected local executable

Codex and Claude may use network access and paid accounts. Open Skeleton cannot promise that a third-party provider is free. The local-command adapter can execute arbitrary code; use only commands you trust and keep its workspace separate from the target repository.
