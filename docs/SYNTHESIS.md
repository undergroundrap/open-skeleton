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
directory. `run-synthesis-plan` validates the frozen plan and prepares exact request
hashes without contacting a provider by default:

```powershell
open-skeleton run-synthesis-plan C:\path\to\repository --provider codex
```

`--execute` is required to contact Codex, Claude, or an explicitly selected local
command. Execution supports 1–16 workers, exact-request resume, and atomic per-job
receipts. Context packets are limited to 100 claims, 200,000 declared characters, and
1 MB of serialized content. Provider workspaces and results must remain outside the
analyzed repository and every Git worktree; a local provider command cannot point to
target-repository code.

After every frozen job has one complete receipt, `assemble-synthesis` validates exact
plan and job coverage, rejects invented claim IDs, and renders a separate narrative
projection:

```powershell
open-skeleton assemble-synthesis C:\path\to\repository `
  --results-dir C:\private\synthesis-runs\codex-cli
```

The static assembly step contacts no model. It preserves section numbers and titles,
summaries, narratives, claim IDs, caveats, conflicts, and unknowns in
`source-grounded-synthesis.md`. It refuses to overwrite deterministic `spec.md` and
also refuses target-repository or Git-worktree output paths.

## Why this can be faster

The local pass reads each source file once and reuses the resulting claims across every
section. Narrative workers receive only relevant, content-pinned context rather than a
full checkout. The work packets are independent, so their elapsed time is bounded by
the slowest packet instead of their sum when an external orchestrator runs them in
parallel. Repeated runs can also reuse unchanged snapshot evidence.

This design does not assume that a model is better at repository census. Its useful job
is narrower: connect already verified facts into an explanation, state consequences,
and make uncertainty readable.

## Proving whether the conclusions are truly present

Fact coverage and heading coverage cannot answer whether two documents reach the same
conclusion. The strict parity workflow begins with a loss-accounted corpus instead:

```powershell
python benchmarks\comparison\run_parity_inventory.py `
  --baseline C:\path\to\registered\tech_spec.md `
  --baseline-id external-single-player-ai-mud-2026-08-04 `
  --candidate C:\path\to\spec.md `
  --repo C:\path\to\pinned\repository `
  --context C:\path\to\codebase_context.md `
  --output-dir C:\private\parity-corpus
```

The inventory verifies the registered artifact and clean repository revision, freezes
the candidate and context hashes, and assigns every nonblank baseline and candidate
line exactly once. Its blocks include prose, headings, lists, table rows, code, and
diagrams. Because it contains private baseline text, it refuses to write inside the
analyzed repository, this tool repository, or any Git worktree.

The next command is a dry run by default. It freezes a rubric and creates deterministic
batches without contacting a model:

```powershell
python benchmarks\comparison\run_agent_parity.py `
  --corpus C:\private\parity-corpus\parity-corpus.json `
  --output-dir C:\private\parity-review
```

Passing `--execute` is the explicit paid-provider action and requires
`--codex-model` and `--claude-model` (or the corresponding option for a selected
single reviewer), so the model identifiers become part of the frozen plan. Claude and
Codex then review the same batches independently in fresh, restricted sessions. Each
sees the complete candidate corpus and neither sees the other's output. The strict result contract
requires complete unit coverage, evidence IDs, candidate block IDs, a rationale,
materiality, baseline validity, and one of `equivalent`, `candidate_superset`,
`partial`, `missing`, `contradictory`, `baseline_incorrect`, `unjudgeable`, or
`not_applicable`. Exact agreement becomes an agent-consensus proposal; disagreement
becomes disputed. Duplicate, missing, malformed, or denominator-changing results fail
closed.

Two models agreeing is still not proof. Generate the human work file only after the
provider proposals are frozen:

```powershell
python benchmarks\comparison\run_parity_gate.py `
  --corpus C:\private\parity-corpus\parity-corpus.json `
  --proposals C:\private\parity-review\parity-review-proposals.json `
  --output-dir C:\private\parity-proof `
  --write-template
```

The template prefills exact consensus but requires a named human to inspect every
block and attest that every semantic atom was checked. Any decision that marks the
baseline incorrect needs a distinct second human and repository evidence IDs. Running
the gate again with `--adjudications` produces `parity_proven: true` only when every
supported material block is human-verified as equivalent or a supported candidate
superset. The receipt is scoped to the exact hash-pinned fixture; it cannot establish
universal superiority or natural-discovery parity on unseen repositories.
