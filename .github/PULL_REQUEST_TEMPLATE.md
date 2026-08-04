## Summary

<!-- What changes, and why. One or two sentences. -->

## Linked issue

<!-- Fixes #NNN, or "none" for a self-contained change. -->

## Type of change

- [ ] Bug fix
- [ ] New analyzer or probe capability
- [ ] Spec profile change
- [ ] Documentation
- [ ] Build, CI, or tooling

## Evidence invariants

- [ ] No claim is emitted without at least one evidence receipt
- [ ] Verified syntax facts stay separate from inference and runtime claims
- [ ] Coverage limitations are documented rather than guessed
- [ ] Nothing executes target repository code, installs its dependencies, or
      reaches the network on the deterministic path
- [ ] The analyzed repository is not modified

## Tests

- [ ] One deterministic positive test
- [ ] One negative or adversarial test

## Verification run

```text
python -m compileall -q src tests benchmarks
python -m unittest discover -s tests
python -m ruff check src tests
python -m mypy
```

<!-- Paste the result lines, including the test count. -->

## Benchmark

Required if this changes analyzer accuracy, claim text, or the standard profile:

```text
python -m open_skeleton benchmark <fixture> --gold benchmarks/single-player-ai-mud/gold.json --output-dir <out>
```

<!-- Paste recall / precision / evidence correctness. Do not make superiority
     claims without a benchmark run. -->

## Clean-room confirmation

- [ ] This change contains no vendor-confidential source, hidden prompts, copied
      proprietary expression, credentials, or material obtained by bypassing
      access controls
