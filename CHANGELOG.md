# Changelog

All notable changes will be documented here. This project follows Semantic Versioning once tagged releases begin.

## [Unreleased]

### Added

- `open-skeleton spec`: outline-driven long-form specifications projected from the
  claim ledger, with user-editable JSON profiles and no model in the path
- 90-section standard profile covering functional surface, state ownership,
  integration, security posture, verification, and enterprise delivery concerns,
  with roughly 700 technology terms across APM, cloud, provisioning, CI,
  brokers, identity, payments, analytics, and feature-flag ecosystems
- cross-cutting concern sections a platform team gates on before adoption:
  rate limiting, retry and circuit breaking, request correlation, pagination,
  idempotency, health and readiness signals, process managers, audit artifacts,
  configured quality gates, and session handling
- declared-surface extraction the ledger held but never projected: model fields
  from annotated classes, function signatures with defaults as written, returned
  payload shapes, object literal field names, and imported names per module
- runtime-reach extraction: platform API reached from outside a module, and
  third-party hosts named in string literals, which no dependency manifest shows
- numeric literals hardcoded inside function bodies, which nothing else indexes
- a complete, untruncated symbol inventory in `spec.json`, with a short form
  matching the spelling imports and stack traces use
- deeper TypeScript extraction: value bindings, destructuring, class and
  interface members, enum members, and object literal keys
- Rust extraction brought to the same families: `const`/`static` tunables with
  their declared types, struct fields, impl methods attributed to the type
  rather than the trait, shared statics as process-local state, the Result and
  `?` error surface, and trait implementations as satisfied contracts
- TypeScript claim families matching Python and Rust by name rather than by
  language: module-scope state written at runtime, `process.env` reads in both
  access forms, and thrown types
- substitute analysis: an absent concern names the structure doing its job,
  with two caveats printed every time — a substitute is a structural
  resemblance rather than an equivalence, and nothing recommends adopting the
  product it stands in for
- a security control matrix consolidating twelve controls into one table, which
  distinguishes a control checked and found missing from one never checked
- an endpoint catalog giving each route its handler's guard count, HTTP
  refusals, and response field names
- module-level data flow: where data enters, rests, and leaves, stated at the
  granularity the call edges actually support
- per-section provenance naming the files each section's conclusions were read
  out of
- `benchmarks/generalization/run_generalization.py`, which measures whether this
  analyses repositories or one repository, and reports yield per file each
  analyzer actually read so a weak analyzer is distinguishable from clean code
- `benchmarks/comparison/run_structure_diff.py`, which compares what two
  specifications are *about* rather than what they name
- re-runnable applicability probes with four verdicts (`applicable`, `degenerate`,
  `absent`, `structural`); an absent concern prints the query that found nothing
- a numeric tunable index and a consolidated failure-response surface, both
  assembled from facts the analyzer already collected and discarded
- per-capability dossiers assembling every record that touches one capability
  into a single briefing, adding no fact
- derived engineering consequences: rules that compose verified claims into
  what follows from them, each citing every claim it rests on
- lexical state value domains for JavaScript and TypeScript, bringing the
  diagram inventory to 83
- `benchmarks/comparison/run_comparison.py`, which counts both documents on
  disk rather than asserting a comparison
- `benchmarks/comparison/run_fact_coverage.py`, which enumerates every fact a
  baseline asserts and reports which are missing by name; `--repo` separates
  facts about the analyzed code from names asserted absent from it, since
  matching the second kind means reproducing a vendor checklist rather than
  extracting anything
- `benchmarks/comparison/run_questions.py`, scoring both documents against
  maintainer questions whose ground truth came from source, not from either
  document
- `scripts/gate.py`, which runs every check CI runs against the local
  interpreter, so a failure is found before it costs metered minutes
- guard-and-exit flowcharts for non-route functions, and a real `erDiagram`
  for durable storage in place of a flowchart approximation
- observed value-assignment state diagrams, each edge labelled with the real
  enclosing condition and its line
- per-route handler guard-and-exit flowcharts drawn from AST guards, raises and
  returns, each node carrying its source line
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

- the dashboard is monochrome; status is carried by weight, border and label
  text rather than hue, so a finding reads the same in grayscale

- the specification opens with an executive summary that leads with decisions
  required — conflicts, untraced capabilities, top findings, absent concerns,
  and the analyzers whose coverage is high but whose yield is low

- analyzer coverage now reports a claim-yield column beside it. Coverage means
  "the file parsed"; yield means "the file produced a finding". Reporting only
  the first overstated how much an analyzer understood
- ledger schema 4 stores per-analyzer claimed-file counts
- `ruff format` is now authoritative for layout, and CI enforces it
- ruff runs a broad rule set (bugbear, bandit, pathlib, naming, simplify,
  performance and more) instead of the previous six-rule selection
- mypy runs in strict mode over both `src` and `tests`

### Fixed

- ledgers written by an earlier schema are migrated additively instead of
  failing on a missing column; a migrated row reports unknown yield rather
  than a fabricated zero

- presence probes no longer count a claim that asserts a counted absence, which
  had inverted the verdict for CI, authentication, testing, and telemetry
- dashboard rejects an unrecognized claim status instead of passing it through
- the reference census no longer reports callback parameters as platform API;
  `mobs.map(m => m.respawn_at)` made `m` look like a global and outranked every
  real call
- TypeScript members are no longer confused with parameters: braces alone put a
  parameter at class-member depth, so `resolveTick(dt)` recorded `dt` as a field
- a binding below a container body is recorded as a local rather than dressed up
  as a member of whatever class encloses it
- numeric literals keep the form they were written in, so `300.0` no longer
  renders as `300` and lose the point that says it is a float
- nested functions no longer leak literals into their parent: `ast.walk` queues
  children before the caller sees the node, so skipping a nested definition
  still yielded everything inside it
- the MCP protocol test no longer relies on iterating a result object, which
  yields `(field, value)` pairs rather than tools when the SDK is installed
- every copy-pasteable command in the README: escape sequences had been
  interpreted at some point, replacing backslashes with tabs and form feeds and
  splitting commands across lines

### Security

- no target execution/network access in deterministic analysis
- symlink, secret, binary, encoding, size, permission, Host, provider-schema, and claim-reference controls
