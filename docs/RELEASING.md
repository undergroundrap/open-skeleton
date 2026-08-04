# Releasing

No release or remote publication is authorized by this document.

When Ocean Bennett explicitly authorizes a release:

1. Confirm the worktree contains only intended changes.
2. Run compile, full tests, Ruff, Mypy, dependency audit, scaling smoke, pinned benchmark, and wheel build.
3. Confirm the MCP protocol test did not skip in the release environment.
4. Update `CHANGELOG.md` and set matching versions in `pyproject.toml` and `src/open_skeleton/__init__.py`.
5. Inspect wheel contents and install it into a clean environment.
6. Create a scoped Conventional Commit.
7. Push only after separate explicit approval.
8. Create a draft release with checksums and benchmark limitations; publish only after review.

Never include local ledgers, provider runs, benchmark baseline artifacts, credentials, `.env` files, or private repository excerpts in a distribution.
