# Contributing

Contributions should preserve the evidence-first and local-first invariants in `AGENTS.md`.

## Before changing code

- inspect `git status --short`
- never add secrets or private repositories
- add one deterministic positive test and one negative/adversarial test for analyzer behavior
- keep verified syntax facts separate from inference and runtime claims
- document coverage limitations rather than guessing

## Verification

```powershell
python -m compileall -q src tests benchmarks
python -m unittest discover -s tests -v
ruff format --check src tests benchmarks
ruff check src tests benchmarks
mypy
python benchmarks\scaling\run_scaling.py
```

`ruff format` is authoritative for layout, so run it rather than hand-aligning.
`mypy` runs in strict mode over both `src` and `tests`, so a new function without
annotations fails. Ruff runs a broad rule set including the bandit security
checks.

Suppress a rule only with an inline `# noqa: <CODE>` plus a comment saying why,
or with a per-file entry in `pyproject.toml` carrying the same justification. A
bare suppression will be sent back.

Run the pinned benchmark before making accuracy or superiority claims. Use scoped Conventional Commits such as `feat(analyzer): detect documented route drift`.

## Adding a language

Language coverage is the most useful contribution this project can receive.
The extension contract is one Protocol and five record types; see
[docs/ADDING_AN_ANALYZER.md](docs/ADDING_AN_ANALYZER.md). State your accuracy
tier honestly — native parser, lexical, or supplied index — and give each of
your language's tokenizer traps a test.

## Clean-room requirement

Do not contribute vendor-confidential source, hidden prompts, copied proprietary expression, credentials, or material obtained by bypassing access controls. Public factual behavior may inform independent design; cite public sources in documentation where useful.
