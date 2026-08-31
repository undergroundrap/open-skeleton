# Parity Execution Plan

This is the dependency order for reaching full-document semantic parity while keeping
repository search and reconciliation local, deterministic, and inexpensive. It is the
handoff document for the next development session.

## What “parity” means

Parity is not similar headings, word count, or a model saying that two documents feel
equivalent. For one registered fixture, strict document parity means:

1. every material semantic atom in the external baseline is inventoried exactly once;
2. the candidate contains an equivalent or stronger supported conclusion;
3. every candidate conclusion is traceable to repository evidence or separately pinned
   ingestion context;
4. two blinded model reviewers may propose mappings, but a human adjudicates every atom;
5. the fail-closed parity gate accepts the exact repository, baseline, candidate, rubric,
   provider result, and adjudication hashes.

That proves parity for one frozen fixture. Product-level confidence additionally needs
both registered fixtures and at least one unseen repository. Natural-discovery parity
and context-assisted document parity stay separate throughout.

## Current stopping point

The foundation is complete enough to deepen rather than redesign:

- bounded local scan, multi-language readers, graph, atomic claims, receipts, conflicts,
  stale projection, profiles, panels, diagrams, and coherence gates;
- deterministic benchmark with complete material-gold coverage on the pinned AI-MUD
  fixture;
- loss-accounted full-document inventory, blinded dual-review planning, and human
  parity gate;
- bounded synthesis jobs that let a model explain ledger evidence without receiving the
  target checkout;
- route recovery across supported frameworks and an HTTP contract concordance joining
  served routes, recognized Markdown API tables, exact client paths, and literal dynamic
  prefixes. The complete matrix carries claim and evidence IDs in `spec.json`.

Strict one-to-one full-document parity has **not** been proven. The next work is the
ordered program below, not presentation expansion or autonomous implementation.

## Execution order

| Order | Work package | Deterministic engine owns | Bounded model owns | Exit proof |
|---:|---|---|---|---|
| 1 | Contract concordance beyond HTTP | Explicit contract identities and fields across model declarations, JSON Schema/OpenAPI, SQL DDL, constructors, CLI choices, registries, docs, and tests; matched, missing, and divergent rows | Explain the consequence of a frozen discrepancy packet | Positive and adversarial tests for every join; no similarity-only joins; matrices rendered on both fixtures |
| 2 | Ownership and shared-facade topology | Resolve supported imports/calls, identify state/store owners, shared facades, bypass paths, and fan-in/fan-out | Explain responsibility boundaries and supported alternatives | Every topology statement has a path of edge/evidence IDs; ambiguous targets remain unresolved |
| 3 | State lifecycle and integrity chains | Identity, create/read/update/delete, persistence, cache ordering, retention, pruning, invalidation, migration, recovery, transaction scope, rollback, orphan paths, and authoritative-state boundaries | Explain failure consequences only from a complete or explicitly incomplete chain | Known fixture motifs are recovered; broken-chain mutations are detected; no consequence outruns its chain |
| 4 | Verification intent and operability | Map tests/harnesses to contracts and capabilities; extract assertions, fixtures, failure cases, logs, metrics, readiness, build, deploy, and runbook paths | Summarize confidence and operational exposure | A capability reports what is verified, how, and what remains unobserved; absence is coverage-bounded |
| 5 | Actor, interface, and decision packets | Inventory UI entry points, commands, public APIs, actors named in repository docs, and evidence required for flows/state machines | Produce use-case narratives and ADR/trade-off packets without inventing intent | Every actor/decision statement cites code or pinned context; unsupported diagrams are explicitly omitted |
| 6 | Micro-template synthesis | Package applicability, inventory, relationships, constraints, negative space, consequences, verification, and references per obligation | Turn one packet into readable prose while preserving conflicts and unknowns | Assembly rejects missing atoms, invented IDs, conflicting projections, and unaccounted jobs |
| 7 | Two-fixture strict parity | Freeze fresh candidates and complete both registered parity corpora | Two blinded reviewers propose atom mappings | Named human adjudication yields fixture-scoped `parity_proven: true` for both, or a machine-readable backlog of every failure |
| 8 | Unseen-repository validation and product hardening | Run without baseline-seeded rules; measure latency, memory, evidence integrity, unsupported languages, and false joins | Review usability and explanations, not repository discovery | Predeclared gold plus human audit passes on an unseen repository; performance and cost budgets hold |

Do not start work package 3 before package 2 can name owners and edges. Do not expand
narrative volume in package 6 before packages 1–5 produce the missing relationships.
Do not claim product parity after package 7 alone; package 8 is what tests generality.

## Immediate next work order

The next session starts with work package 1 and should stop only after this vertical
slice is complete:

1. define a provider-neutral `ContractSurface` record with explicit identity, fields or
   values, source kind, claim IDs, evidence IDs, and unresolved-link reason;
2. add extractors for the contract forms already structurally available, beginning with
   Python model fields, SQL table columns, JSON Schema/OpenAPI properties, CLI choices,
   and named registry values;
3. join only on an explicit reference or a canonical identity whose normalization is
   specified and tested; emit unresolved candidates instead of choosing by similarity;
4. render a complete machine-readable concordance and a bounded human panel;
5. add at least one positive, one near-name adversarial, one method/type disagreement,
   and one incomplete-contract test per join family;
6. run the result on both frozen fixtures and record which private-baseline atoms it
   closes without storing private text in Git;
7. run the full quality gate and immutable benchmark, then commit one scoped tranche.

The first implementation slice of work package 1—HTTP routes, docs, and local
callers—is complete. Extend its evidence-preserving pattern; do not replace it with a
general name-similarity matcher.

## Agent boundary

Agents should receive packets, not repositories. A packet may ask an agent to:

- explain why a verified mismatch matters;
- compare explicitly supported alternatives;
- connect a bounded set of evidence-backed facts into a mechanism;
- distinguish repository evidence from pinned business context;
- perform blinded parity review against frozen candidate blocks.

An agent must not be the primary repository crawler, invent an ownership or schema
join, convert “not extracted” into “absent,” silently repair contradictions, or change
the parity denominator. If the local engine cannot assemble the evidence packet, that
is an analyzer backlog item rather than a prompt-engineering task.

## Gate used at every stopping point

A tranche is handoff-ready only when:

- the worktree was inspected before editing and unrelated changes remain untouched;
- each analyzer or reconciliation rule has deterministic and adversarial coverage;
- unit/integration tests, formatting, lint, type checking, compilation, and document
  coherence pass;
- the immutable benchmark does not regress recall, precision, evidence integrity,
  conflict detection, or its declared performance budget;
- both registered fixtures were exercised when the change affects document semantics;
- private baseline text and provider output remain outside every Git worktree;
- the roadmap names the next single work package and no unproven parity claim is made.
