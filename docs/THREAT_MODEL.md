# Threat model

## Assets

- source code, documentation, history, and architecture
- credentials and local configuration near a repository
- filesystem integrity of the target and host
- ledger accuracy, provenance, and snapshot binding
- provider credentials, network access, and spend

## Adversaries and failures

- malicious filenames, encodings, oversized files, symlinks, and binary payloads
- repository code designed to execute during inspection or dependency installation
- documentation intended to manipulate a model or contradict source
- stale receipts after a file changes
- model output that invents claim IDs or schema fields
- DNS rebinding or remote exposure of the local dashboard
- overly broad agent permissions or misleading MCP annotations
- benchmark overfitting and selective reporting

## Default controls

- resolve and validate one approved root
- use `os.scandir` and never follow symlinks
- exclude VCS internals, dependencies, builds, analyzer state, common credentials, keys, binaries, and oversized files before content ingestion
- reject non-UTF-8 text and record permission/read failures
- never execute target files, package managers, compilers, tests, or hooks
- never contact the network during deterministic analysis
- use parameterized SQL, foreign keys, transactions, WAL, immutable receipt hashes, and snapshot IDs
- verify the current whole-file and excerpt hash before returning source text
- keep facts, inference, conflict, unknown, and stale status distinct
- bind MCP and dashboard services to one repository root
- keep the default ledger and exports in the OS-local state area outside the target repository
- bind the dashboard to loopback, validate the Host header, reject writes, and send CSP/no-store headers
- mark MCP query tools read-only and disclose refresh as a local ledger write
- require explicit provider choice, bounded packs, strict JSON, and in-pack claim IDs
- pin benchmark commits and preserve limitations beside scores

## Residual risks

- filename-based secret exclusions are not a complete secret scanner
- a very large number of individually allowed files can consume time and memory
- static analysis can miss reflection, dynamic imports, generated code, proxy controls, and runtime configuration
- inferred framework behavior can differ by installed version or middleware
- the explicit local-command provider executes an arbitrary user-selected command
- Codex and Claude adapters can contact third parties and may incur cost
- a local process with filesystem access can read dashboard/ledger data directly

## Out of scope

- automatic source edits or source-control writes
- running untrusted repository code in a sandbox
- hosted multi-user operation
- complete malware or secret detection
- legal conclusions about third-party code or patents
