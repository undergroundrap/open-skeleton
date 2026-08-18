# Architecture

## Trust boundary

The target repository is untrusted, read-only input. Open Skeleton resolves one approved root, walks regular files without following symlinks, excludes known sensitive/generated/binary inputs, hashes each accepted payload, and writes only to a state directory. The default is a deterministic per-repository directory beneath the OS-local state home, outside the target; an explicit `--state-dir` may override it only with another external path.

```text
untrusted repository
  -> bounded scanner + snapshot hash
  -> deterministic language/project adapters
  -> symbols + edges + evidence receipts
  -> cross-adapter conflict and negative-space rules
  -> atomic claim ledger
  -> CLI / dashboard / MCP / concise exports
  -> optional bounded synthesis provider
```

## Modules

- `scanner.py`: traversal, decoding, hashing, roles, exclusions, progress events
- `policy.py`: size, secret, binary, dependency, and build-output policy
- `ignore.py`: the repository's own `.gitignore`, read so generated output is
  identified by what the repository declares rather than by a list of names
- `state.py`: stable, platform-local state paths outside target repositories
- `analyzers/python_ast.py`: Python syntax and selected framework facts
- `analyzers/typescript_lexical.py`: explicitly lexical JS/TS facts
- `analyzers/project_metadata.py`: manifests and documentation claims
- `analyzers/hum_semantic_index.py`: versioned Hum-native graph adapter
- `analysis.py`: deterministic orchestration and cross-adapter conflicts
- `ledger.py`: SQLite persistence, search, evidence verification, diffs, invalidation
- `mcp_server.py`: repository-bound agent service and official-SDK tool registration
- `dashboard.py`: loopback-only, read-only human UI
- `providers.py`: explicit provider request/result boundary
- `benchmark.py`: pinned gold scoring and direct measurements

## Evidence and claim model

An evidence receipt identifies snapshot, normalized path, line span, symbol, evidence kind, producer, and excerpt hash. A material claim identifies its status, confidence, importance, producer, supporting/contradicting receipts, invalidation keys, and alternative hypotheses.

`verified` means the stated syntax or relationship is directly present in the receipt. It does not mean runtime execution occurred. `inferred` preserves alternatives. `conflict` carries evidence on both sides. `unknown` is a valid first-class state. `stale` is a projection against a newer snapshot; historical claims are not rewritten.

## Incrementality

Snapshot identity is a deterministic digest of included path/content metadata and scanner policy. Diffs operate over file hashes. Claims declare file, symbol, graph, or file-set invalidation keys. Changed dependencies project older claims as stale while preserving the historical ledger.

The current implementation reruns deterministic adapters for a refreshed snapshot, then uses invalidation metadata to identify stale prior claims. Fine-grained per-symbol recomputation is a future optimization, not a current performance claim.

## Complexity

For `F` files and `B` included bytes:

- traversal and hashing: `O(F + B)`
- canonical snapshot ordering: `O(F log F)`
- Python AST and JS/TS lexical passes: `O(B)` for supported files
- ledger writes: `O(symbols + edges + evidence + claims)`, excluding index constants
- Markdown report sorting: `O(C log C)` for `C` claims

The scanner holds at most one bounded file payload. Analyzers currently retain normalized semantic records for one snapshot in memory before a transactional ledger write. Measured scaling results are in [PERFORMANCE.md](PERFORMANCE.md).

## Capability separation

Default deterministic analysis has no model or network dependency. MCP adds an optional SDK but does not add repository mutation. Provider adapters are explicit capabilities and receive bounded evidence packs. The local-command adapter is arbitrary execution by user choice; it is not part of scanning or analysis.
