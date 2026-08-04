# Performance and complexity

Synthetic measurements use 100-line Python files containing one module assignment per line. They include scanning, hashing, AST analysis, cross-adapter analysis, and in-memory result construction; they exclude SQLite persistence and export.

August 4, 2026 local measurements:

| Files | Lines | Total time | Traced Python peak |
|---:|---:|---:|---:|
| 100 | 10,000 | 656 ms | 11,394,014 bytes |
| 500 | 50,000 | 3,919 ms | 56,947,348 bytes |
| 1,000 | 100,000 | 8,425 ms | 113,010,185 bytes |

The observed memory ratio from 10k to 100k lines was 9.92x; the time ratio was 12.84x. This is consistent with the intended approximately linear semantic pass plus allocation/index overhead, but three synthetic points are not an asymptotic proof.

The CI smoke budget analyzes 300 files/30,000 lines in under 10 seconds and below 64 MiB of traced allocations. Reproduce the broader measurement with:

```powershell
$env:PYTHONPATH = "src"
python benchmarks\scaling\run_scaling.py
```

Large repositories with many assignment/call sites create many receipts by design. Future work includes streaming ledger writes and bounded per-adapter retention so evidence volume does not require holding a whole snapshot’s records in memory.
