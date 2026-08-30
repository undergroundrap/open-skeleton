# Source-grounded synthesis

Open Skeleton separates repository reading from narrative writing. Local analyzers
build the inventory, graph, claims, conflicts, and immutable source receipts first.
An optional model receives one bounded obligation at a time after that work is
complete. The model explains evidence; it is not asked to rediscover the repository.

## What the two registered examples reveal

The two supplied external specifications describe unrelated repositories, yet both
use the same ten top-level headings and share 45 second-level headings. Their deepest
tables, diagrams, and explanations vary with the repository. This is evidence of a
stable obligation taxonomy followed by repository-specific expansion, rather than one
unstructured agent reading until it decides to stop.

The supplied ingestion contexts also name some desired concerns, such as state
ownership, entry boundaries, operational risks, and documentation conflicts. A result
that repeats one of those concerns may be correct, but it is prompt-seeded discovery.
The comparison tools preserve that distinction instead of crediting every result as
independently found.

The resulting working model is:

1. map files, symbols, relationships, declarations, configuration, and documentation;
2. route those facts through a stable list of specification obligations;
3. deepen each obligation with a small independent narrative task;
4. assemble the results and check their source references and consistency.

The exact private orchestration is not observable from two outputs. This model is the
smallest mechanism consistent with both artifacts and with the public workflow
description; it is not a claim that the original implementation has been copied.

## The faster local path

Repository-wide census work belongs in deterministic analyzers because parsing once is
faster and easier to verify than asking many agents to reread the same files. Open
Skeleton therefore uses this pipeline:

```text
repository
  -> bounded scan and language readers
  -> symbol/relationship graph
  -> atomic claim ledger with two-sided evidence
  -> obligation routing and deterministic consequences
  -> bounded synthesis jobs
  -> optional parallel narrative workers
  -> schema, citation, and coherence gates
```

`plan-synthesis` implements the handoff point. It creates one independent job for
every non-structural outline obligation. Each job contains:

- the obligation, verdict, and probes that established applicability or absence;
- the exact routed claims, including both supporting and contradicting receipts;
- explicit missing or omitted claim IDs when a size bound prevents silent inclusion;
- a task contract requiring claim-ID citations and preservation of conflicts and
  unknowns;
- a priority derived from conflicts, unknowns, unmet requirements, and claim
  importance.

Plan construction does not invoke a model:

```powershell
open-skeleton analyze C:\path\to\repository
open-skeleton plan-synthesis C:\path\to\repository
```

The default artifact is `synthesis-plan.json` in the repository's external state
directory. An orchestrator may dispatch its `parallel_safe` jobs concurrently. Open
Skeleton does not currently dispatch the whole plan itself; provider invocation remains
explicit.

## Why this can be faster

The local pass reads each source file once and reuses the resulting claims across every
section. Narrative workers receive only relevant, content-pinned context rather than a
full checkout. The work packets are independent, so their elapsed time is bounded by
the slowest packet instead of their sum when an external orchestrator runs them in
parallel. Repeated runs can also reuse unchanged snapshot evidence.

This design does not assume that a model is better at repository census. Its useful job
is narrower: connect already verified facts into an explanation, state consequences,
and make uncertainty readable.

## Measuring whether the conclusions are truly present

Fact coverage and heading coverage cannot answer whether two documents reach the same
conclusion. `run_reasoning_inventory.py` creates a one-to-one review queue instead:

```powershell
python benchmarks\comparison\run_reasoning_inventory.py `
  --baseline C:\path\to\registered\tech_spec.md `
  --baseline-id external-single-player-ai-mud-2026-08-04 `
  --candidate C:\path\to\spec.md `
  --repo C:\path\to\pinned\repository `
  --context C:\path\to\codebase_context.md `
  --output-dir reasoning-review
```

The inventory is fence-aware, verifies the registered artifact and repository revision,
marks repository-grounded and prompt-seeded anchors, and retrieves a related candidate
paragraph. Retrieval is only a review aid. It never labels lexical similarity as
semantic equivalence and reports no conclusion-coverage number until every extracted
unit is adjudicated as `equivalent`, `partial`, `missing`, `baseline_incorrect`, or
`unjudgeable`.

This is the current honest boundary: the deterministic engine recovers nearly all
repository-present names in both registered examples, but that does not prove it
naturally reaches every useful conclusion. The review inventory makes the remaining
question finite, attributable, and safe to use as the next analyzer-development queue.
