# Security policy

## Supported versions

Security fixes are currently applied to the latest unreleased `main` branch and the most recent tagged `0.x` release once releases begin.

## Reporting

Do not open a public issue containing an exploit, secret, or private repository content. Once the GitHub repository is published, use its private Security Advisory flow. Until then, contact the maintainer privately through the account named in `pyproject.toml` and share only the minimum reproduction necessary.

## Security invariants

- target repositories are untrusted and read-only
- deterministic analysis does not execute target code or use the network
- symlinks are not followed
- known secrets, private keys, binaries, dependencies, builds, and oversized files are excluded
- source excerpts are returned only after snapshot/file/excerpt validation
- default state is stored outside the analyzed repository
- the dashboard is loopback-only and read-only
- provider use is explicit and schema-bound
- no command automatically edits, commits, pushes, or opens a pull request

See [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for residual risks and non-goals.
