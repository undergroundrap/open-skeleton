# Changelog

All notable changes will be documented here. This project follows Semantic Versioning once tagged releases begin.

## [Unreleased]

### Added

- `open-skeleton spec`: outline-driven long-form specifications projected from the
  claim ledger, with user-editable JSON profiles and no model in the path
- 50-section standard profile covering functional surface, state ownership,
  integration, security posture, verification, and enterprise delivery concerns
- re-runnable applicability probes with four verdicts (`applicable`, `degenerate`,
  `absent`, `structural`); an absent concern prints the query that found nothing
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

### Fixed

- presence probes no longer count a claim that asserts a counted absence, which
  had inverted the verdict for CI, authentication, testing, and telemetry
- dashboard rejects an unrecognized claim status instead of passing it through

### Security

- no target execution/network access in deterministic analysis
- symlink, secret, binary, encoding, size, permission, Host, provider-schema, and claim-reference controls
