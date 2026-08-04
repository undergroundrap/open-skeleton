# Milestone 0: Deterministic Skeleton

**Completed:** August 4, 2026
**Version:** 0.1.0

## Delivered

- New AGPL-3.0 personal repository with visible attribution notice
- Dependency-free Python 3.12 runtime
- Safe read-only traversal and bounded file hashing
- Sensitive, binary, generated, dependency, VCS, and state exclusions
- Deterministic snapshot identities
- Language and role classification, including `.hum`
- SQLite evidence ledger, FTS5 metadata search table, and future claim/evidence schema
- Append-only scan events
- Atomic JSONL and Markdown exports
- CLI `scan` and `status` commands
- CI workflow, architecture, threat model, and roadmap
- Nine passing regression tests
- Successful wheel build

## Verification observations

Results are local observations from Ocean Bennett's Windows development machine, not generalized performance claims.

| Target | Included files | Included lines | Included bytes | Inventory time |
|---|---:|---:|---:|---:|
| `open-skeleton` | 24 | 2,382 | 94,789 | 4 ms |
| `hum-lang` | 523 | 183,480 | 7,421,866 | 214 ms |

The Hum scan recognized 229 `.hum` files, 70 Rust files, and 173 Markdown files. Its state was written beneath `open-skeleton/.open-skeleton/hum-lang`; the Hum repository was not modified.

These runs prove only deterministic inventory and persistence. They do not yet constitute semantic reverse engineering.

## Defect found by tests

The first Windows run exposed an SQLite connection-lifetime bug: using a connection as a context manager commits or rolls back but does not close it. Temporary databases remained locked. The ledger now wraps every connection in an explicit closing session, and the regression suite covers repeated saves and temporary-directory cleanup.

## Next milestone

Implement the Python semantic adapter and immutable evidence receipts:

1. Parse modules, definitions, imports, decorators, calls, and source spans with native `ast`.
2. Extract initial framework facts for FastAPI routes, Pydantic models, SQLite calls, environment reads, and tests.
3. Store normalized symbols, edges, and evidence records.
4. Produce an analysis-coverage report rather than silently skipping unresolved behavior.
5. Evaluate against `SINGLE-PLAYER-AI-MUD` before adding model reasoning.

After the normalized adapter contract is proven, add the TypeScript compiler worker and design the native Hum semantic-index adapter.
