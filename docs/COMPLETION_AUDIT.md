# Completion audit

Audit date: August 4, 2026

Status: **local release candidate**. Deterministic analysis, packaging, the AI-MUD
benchmark, self-analysis, and the local test suite pass. Three release-environment checks
remain intentionally open: the official MCP SDK protocol run, Ruff/Mypy/pip-audit, and a
hands-on visual browser pass. CI is configured for the first two. No remote publication has
been authorized.

## Evidence recorded in this audit

- `python -m compileall -q src tests`: pass
- `python -m unittest discover -s tests -v`: 51 tests, pass, with two
  environment-dependent skips (official MCP SDK absent and real Windows symlink creation lacks
  privilege); the platform-independent symlink branch test passes
- deterministic self-analysis: 389 symbols, 3,491 relationships, 3,453 evidence receipts,
  43 claims, zero emitted conflicts, zero emitted unknowns; all four adapter coverage records
  report 100% for their eligible files
- pinned AI-MUD release-candidate benchmark: 33/33 material findings, 100% material recall,
  100% scoped precision, 100% evidence correctness, 100% conflict detection
- benchmark timing: 691 ms to first finding, 1,247 ms total, 10,699,295 bytes traced peak
- scaling: 10k lines in 656 ms, 50k in 3,919 ms, 100k in 8,425 ms
- dependency-free wheel: `open_skeleton-0.1.0-py3-none-any.whl`, SHA-256
  `aa4ccf05c79e1516332b816d0039fbf4bf72383374c7bddf642b8fc05671f114`;
  includes both public JSON schemas, installed without dependencies into a clean virtual
  environment, scanned a one-file fixture into an external local-state root, and left the
  fixture repository unchanged

## Gate status

### G1: Safe repository boundary - pass locally

- Scanner is read-only, resolves one approved root, never follows symlinks, and performs no
  network or target-code execution.
- Secret, binary, oversized, malformed UTF-8, permission-error, and content-change tests pass.
- The real Windows symlink fixture may skip without Developer Mode, so a platform-independent
  mock test also proves the `is_symlink` branch excludes before directory/file inspection.
- MCP services bind the root at construction. Default and explicit state paths inside the target
  are rejected. Providers receive a bounded context pack and use a separate state workspace.

### G2: Semantic evidence - pass locally

- Python AST, TypeScript lexical, project metadata, and Hum semantic-index adapters emit
  versioned coverage.
- Every source receipt is content-pinned and verified before excerpt retrieval.
- Unsupported Hum without a native graph reports exact zero semantic coverage and explicitly
  records that the compiler was not executed.

### G3: Claims and conflicts - pass locally

- All five statuses are modeled and persisted with supporting and contradicting evidence,
  alternatives, confidence, importance, and invalidation keys.
- Snapshot changes project affected historical claims as stale.
- The pinned fixture covers routes, state ownership, security boundaries, documentation drift,
  dependency drift, tests, runtime topology, AI failure behavior, and mathematical conflict.

### G4: Agent-native access - CI confirmation pending

- CLI and repository-bound MCP domain-contract tests pass.
- Read tools and the ledger-writing refresh tool have distinct annotations.
- Codex, Claude, local-command, and disabled adapters share a strict schema. Mocked command tests
  prove Codex uses ephemeral/read-only mode and Claude denies repository, network, and mutation
  tools.
- The official MCP client lifecycle test is present but skips in the minimal local runtime
  because the optional SDK cannot be installed in this session. Windows and Linux CI install the
  `mcp` extra and run the test as mandatory.

### G5: Human product experience - functional pass; visual pass pending

- Analyzer events expose stages, elapsed time, and time to first finding.
- HTTP tests verify the loopback-only, read-only dashboard, Host validation, CSP without
  `unsafe-inline`, native coverage progress, summary, claims, coverage, evidence, and diff APIs.
- Browser visual QA was attempted, but the in-app browser security policy denied loopback access.
  No alternate browser route was used. Desktop/mobile appearance remains a release checklist
  item rather than an unverified claim.

### G6: Engineering quality - CI confirmation pending

- Compile, 51-test final suite, self-analysis, dependency-free wheel build, benchmark, and
  measured 100k-line scaling are local release requirements.
- CI runs Windows/Linux tests, Ruff, Mypy, pip-audit, source/wheel builds, the official MCP
  protocol contract, and the pinned public benchmark.
- Ruff, Mypy, and pip-audit were not available in the bundled offline runtime, so their CI result
  must be green before a tag.

### G7: Comparative benchmark - pass for the stated scope

- Fixture commit, gold schema, 33 material claims, baseline artifact hash, analyzer versions,
  receipts, timings, allocations, and output volume are machine-readable.
- Results explicitly limit precision to enumerated categories and identify the author-reviewed,
  single-fixture, `tracemalloc`, and manual-baseline limitations.

### G8: Independent development and release readiness - pass locally

- Provenance, threat model, security policy, contribution rules, changelog, release checklist,
  AGPL license, and attribution notice are present.
- The supplied baseline artifact is hash-referenced but not redistributed.
- No confidential source, hidden prompt, credential, remote push, release, or repository setting
  change is included.

## Release decision

Do not tag or publish until the official MCP protocol test, Ruff, Mypy, pip-audit, and visual
dashboard checklist are complete. The current artifact is suitable for continued local use and
for building Hum semantic-graph exporters against the documented schema.
