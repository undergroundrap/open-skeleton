# Independent-development provenance

Open Skeleton was designed from:

- public product and protocol documentation
- standard AST, lexical analysis, dependency graph, RAG, SQLite, MCP, and agent-orchestration patterns
- direct source inspection of repositories Ocean Bennett owns or is authorized to analyze
- two user-supplied commercially generated technical specifications used as external benchmark artifacts
- observed public product behavior during an authorized application exercise

The repository does not contain any third-party vendor's source code, internal prompts, private API responses, credentials, confidential documents, copied UI assets, or decompiled binaries. Benchmark notes paraphrase factual findings and record measurements; the two baseline artifacts themselves are not redistributed. Their canonical Markdown exports are identified only by SHA-256, byte count, repository, and best-known revision in `benchmarks/comparison/baselines.json`. The PDFs shipped beside those Markdown files are alternate renderings of the same two exports, not additional baseline runs.

Implementations and names were created independently for Open Skeleton. Compatibility is limited to public formats or command interfaces such as Python ASTs, JSON Schema, SQLite, MCP, Codex CLI, Claude Code CLI, and the documented Hum semantic graph.

Contributors must not submit confidential employer/vendor material, leaked prompts, access-control bypasses, or code obtained in violation of terms or law. Patent and license questions require qualified legal counsel; this document is engineering provenance, not legal advice.
