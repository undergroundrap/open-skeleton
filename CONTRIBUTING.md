# Contributing

Contributions should preserve the evidence-first and local-first invariants in `AGENTS.md`.

## Before changing code

- inspect `git status --short`
- never add secrets or private repositories
- add one deterministic positive test and one negative/adversarial test for analyzer behavior
- keep verified syntax facts separate from inference and runtime claims
- document coverage limitations rather than guessing

## Verification

Install the package first. Several checks — the MCP protocol tests, and the
type check over code that imports the SDK — behave differently when the
optional dependencies are absent, and skipping the install makes them silently
weaker rather than loudly missing:

```powershell
python -m pip install -e ".[dev,mcp]"
```

Then run every gate CI runs, locally:

```powershell
python scripts\gate.py --full
```

`--full` adds the dependency audit and the distribution build, both of which
need a network. Without it those two are skipped and the run says so. Add
`--fix` to apply formatting and lint autofixes instead of only reporting them.

The individual commands, should you want one of them on its own:

```powershell
python -m compileall -q src tests benchmarks
python -m unittest discover -s tests -v
ruff format --check src tests benchmarks
ruff check src tests benchmarks
mypy
python benchmarks\scaling\run_scaling.py
```

A local run cannot substitute for the operating-system matrix: CI runs Ubuntu
and Windows, and a path-separator or line-ending fault will only appear on the
one you are not using.

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
