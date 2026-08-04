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
ruff check src tests
mypy src
python benchmarks\scaling\run_scaling.py
```

Run the pinned benchmark before making accuracy or superiority claims. Use scoped Conventional Commits such as `feat(analyzer): detect documented route drift`.

## Clean-room requirement

Do not contribute vendor-confidential source, hidden prompts, copied proprietary expression, credentials, or material obtained by bypassing access controls. Public factual behavior may inform independent design; cite public sources in documentation where useful.
