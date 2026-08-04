# Completion Gates

The full project goal is achieved only when every required gate below has authoritative evidence.

## G1: Safe repository boundary

- Default analysis performs no target execution or network access.
- Symlink, traversal, secret-file, binary, oversized-file, permission-error, and malformed-encoding tests pass.
- MCP and provider tools cannot silently widen repository permissions.

## G2: Semantic evidence

- Python and TypeScript adapters emit symbols, relationships, and source receipts.
- The Hum adapter consumes a versioned native semantic index or reports a precise coverage limitation.
- Every receipt verifies its file hash against the analyzed snapshot.
- Analysis coverage and unresolved facts are queryable.

## G3: Claims and conflicts

- Atomic claims support all five statuses.
- Supporting and contradicting evidence are preserved.
- Documentation drift, durable/process-local state, routes, configuration, tests, and missing neighboring controls are benchmarked.
- Changed evidence marks dependent claims stale and triggers bounded recomputation.

## G4: Agent-native access

- CLI supports scan, analyze, query, diff, export, and benchmark workflows.
- Local MCP server passes initialization, tool-list, representative call, invalid-input, and shutdown tests.
- Query tools are accurately annotated read-only; analysis tools disclose local ledger writes.
- Codex, Claude, and local provider adapters share one structured request/result contract and can be disabled entirely.

## G5: Human product experience

- Progressive events expose time to first finding and current stage.
- A local dashboard presents findings, evidence, conflicts, unknowns, coverage, and snapshot changes.
- Artifacts are concise by default and link every material claim to evidence.

## G6: Engineering quality

- Unit, integration, contract, adversarial, and benchmark tests pass on Windows and CI.
- Packaging installs without downloading runtime dependencies for deterministic-only operation.
- Static checks, type checks, dependency/security checks, and performance smoke tests are automated.
- Complexity budgets are documented and measured rather than asserted.

## G7: Comparative benchmark

- The benchmark pins the repository commit, analyzer versions, prompts, and source artifacts.
- Gold claims are independently adjudicated and machine-readable.
- Both systems are scored for recall, precision, evidence correctness/coverage, conflicts, state, negative space, confidence, latency, and output volume.
- Results distinguish direct measurements from interpretation.

## G8: Independent-development and release readiness

- Provenance and clean-room boundaries remain documented.
- No third-party confidential material or copied protected expression is present.
- License, NOTICE, security policy, contribution guide, changelog, and release instructions are complete.
- No remote publication occurs without explicit authorization.

