# Changelog

All notable changes will be documented here. This project follows Semantic Versioning once tagged releases begin.

## [Unreleased]

### Added

- `open-skeleton spec`: outline-driven long-form specifications projected from the
  claim ledger, with user-editable JSON profiles and no model in the path
- 55-section standard profile covering functional surface, state ownership,
  integration, security posture, verification, and enterprise delivery concerns
- re-runnable applicability probes with four verdicts (`applicable`, `degenerate`,
  `absent`, `structural`); an absent concern prints the query that found nothing
- per-route Mermaid sequence diagrams built from handler-scoped call edges,
  and a persistence entity diagram built from durable-table claims
- implemented-capability catalog clustered from route prefixes and package
  structure, with a traceability matrix and a verification-gap report
- Rust lexical analyzer: `unsafe` surface, panicking call sites, `#[test]`
  census, items and imports, with nested block comments, hashed raw strings
  and lifetimes handled explicitly
- spec sections 6.6 Memory-Safety Escape Hatches and 7.4 Panicking Call Sites
- `--hum-index` is repeatable, so whole-repo Hum coverage can be sharded across
  several indexes; each keeps its own hashed receipt
- `references_route_path` edges from Python route-path string literals, so
  traceability can follow HTTP exercise rather than only direct calls
- `dependency_name` and `import_target` probe kinds for library-level detection
- repository composition panels: language and role census, largest files, and a
  counted exclusion report
- `--verify` citation integrity: every receipt re-resolved against current source
  bytes, exiting non-zero on any failure
- PEP 621 `pyproject.toml` dependency extraction
- positive `auth_control` claim so a repository with route auth controls produces
  a claim, not only one when they are missing
- bounded repository scanner and SQLite evidence ledger
- Python, JavaScript/TypeScript, project metadata, and Hum semantic-index adapters
- evidence-backed five-state claims, conflicts, diffs, and stale projection
- CLI, loopback dashboard, repository-bound MCP service, and optional provider adapters
- pinned comparative benchmark and synthetic performance harness
- Windows/Linux CI, package build, lint, type, dependency-audit, and protocol gates

### Changed

- analyzer coverage now reports a claim-yield column beside it. Coverage means
  "the file parsed"; yield means "the file produced a finding". Reporting only
  the first overstated how much an analyzer understood
- ledger schema 4 stores per-analyzer claimed-file counts
- `ruff format` is now authoritative for layout, and CI enforces it
- ruff runs a broad rule set (bugbear, bandit, pathlib, naming, simplify,
  performance and more) instead of the previous six-rule selection
- mypy runs in strict mode over both `src` and `tests`

### Fixed

- presence probes no longer count a claim that asserts a counted absence, which
  had inverted the verdict for CI, authentication, testing, and telemetry
- dashboard rejects an unrecognized claim status instead of passing it through

### Security

- no target execution/network access in deterministic analysis
- symlink, secret, binary, encoding, size, permission, Host, provider-schema, and claim-reference controls
