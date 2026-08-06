# Specification Projection

`open-skeleton spec` renders a long-form technical specification by projecting the
evidence ledger through an **outline profile**. It generates no claims. Every
statement in the output already existed in the ledger, and every section heading
came from a data file the user can edit.

```powershell
open-skeleton analyze C:\path\to\repository
open-skeleton spec C:\path\to\repository --verify
```

This writes three files into the state directory. `spec.md` is the readable
document, `spec.json` is the same document as data, and `spec.index.json`
carries the complete symbol inventory and name concordance.

The inventories are split out because they scale with the repository rather
than with what is interesting in it: on a 523-file tree they were 37% of a
six-megabyte file that a consumer had to parse in full to read one section.
An agent that wants every name still gets it; one that wants the document is
no longer charged for it.

## Why a profile instead of a prompt

Long-form generators normally encode their outline inside a model prompt. That
makes the outline unauditable, unversionable, and identical for every repository.
Here the outline is a JSON document with a validated schema, so a team can encode
its own review checklist and diff it like code.

The packaged outline is `src/open_skeleton/spec/profiles/standard.json`. Its nine top-level sections follow conventional specification taxonomy — the
ISO/IEC/IEEE 29148 requirements structure, C4 architectural levels, and a standard
operational-readiness checklist. Supply your own with `--profile`.

## The report leads with decisions, not measurements

A long specification is unusable if the reader has to locate the important parts
themselves. The executive summary is rendered first and answers one question:
what needs a decision.

| Block | Contents |
|---|---|
| Decisions required | Unresolved conflicts, plus capabilities no test or harness reaches |
| Contradictions between sources | Each conflict with its first receipt and the section that carries the rest |
| Highest-importance verified findings | Critical and high claims, evidence-linked |
| Concerns not implemented | Absent determinations, counted against the concerns actually probed |
| Where this analysis is thin | Analyzers with full coverage and low yield |

Everything in it is selected from claims rendered below, so it is a view rather
than a second source of truth, and every row points at the section holding the
receipts.

The last block is the one competitors do not write. It names the analyzers that
read every eligible file and produced almost nothing, so a reader knows which
sections rest on a thin read. That is a limit of the tool, stated by the tool,
in the document the tool produced.

## Determination: absence is a verdict, not a silence

Each outline node declares **probes** — named, re-runnable queries over the pinned
snapshot. The verdict follows from counted matches alone:

| Verdict | Meaning |
|---|---|
| `applicable` | At least one probe matched. |
| `degenerate` | Probes matched, but below the node's `degenerate_below` threshold. |
| `absent` | Every declared probe returned zero matches. |
| `structural` | The node declares no probes; it only organizes its children. |

An `absent` section still prints its probe table, so a reader can re-run the query
that found nothing instead of trusting the conclusion. This is the difference
between "the specification does not mention containers" and "these six globs
matched zero paths in snapshot `abc123`."

### Probe kinds

| Kind | Queries |
|---|---|
| `path_glob` | Snapshot file paths, matched against full path and basename |
| `file_language` | Detected language of included files |
| `file_role` | Scanner-assigned role (`source`, `test`, `documentation`, …) |
| `claim_category` | Claims in the named categories |
| `sourced_claim_category` | Claims in the named categories **backed by a real file receipt** |
| `symbol_kind` | Extracted symbols by kind |
| `edge_relationship` | Relationship edges by kind |
| `dependency_name` | Declared dependency names from project manifests |
| `import_target` | Modules imported by source files |

`dependency_name` and `import_target` glob-match every reasonable spelling of a
library: the target itself, an npm scope stripped (`@opentelemetry/api` matches
`opentelemetry*`), the final path segment, and the leading dotted segment
(`opentelemetry.trace` matches `opentelemetry*`). They never match across
relationships — a `dependency_name` probe cannot be satisfied by an import.

This pair is what makes enterprise-concern detection real: §8.5 Telemetry, §8.6
Error Tracking, §8.8 Managed Cloud Services, §6.3 Cryptography, §5.5 Asynchronous
Messaging, and §4.4 Caching are all decided by asking whether the relevant client
library is declared or imported, and reporting the exact list of names queried.

### Why `sourced_claim_category` exists

Analyzers record counted absences as claims — "no CI workflow exists under
`.github/workflows`" is a `delivery_automation` claim with a repository-wide census
receipt whose path is `.`. A naive `claim_category` probe counts that claim and
concludes CI is *present*, inverting the finding.

`sourced_claim_category` counts a claim only when at least one supporting receipt
points at an actual file. Use it for any concern where the analyzer may speak up
specifically to report that something is missing. The standard profile uses it for
delivery automation, authentication controls, testing, observability, and every
drift category.

## Claim routing

Each node's `findings` selector pulls claims by category, status, and minimum
importance. A claim is consumed by the first node that selects it, so the document
never counts one finding twice. Section 9.4 has a selector with no filter, which
catches anything the outline failed to route — a non-empty 9.4 means the profile
needs a new section, not that the analysis was incomplete.

`constraints` selectors work differently: they are not consumed, and they render
only under an `absent` verdict. This is how §8.3 Orchestration shows the
process-local state and single-connection storage claims that a second replica
would violate — observations drawn from elsewhere in the ledger, not
recommendations.

## Composition panels

A node may declare `panels`, which report what the scanner and analyzers
recorded rather than what a claim concluded. A panel renders a table or nothing;
it never asserts.

Composition — what the scanner saw:

| Panel | Reports |
|---|---|
| `snapshot_totals` | File, line, and byte counts; language and role variety; scan duration; policy version |
| `language_census` | Files, share, lines, and bytes per language |
| `role_census` | The same breakdown per scanner-assigned role |
| `largest_files` | The largest included files with their content hashes |
| `exclusions` | Every excluded entry grouped by reason |

Capabilities — what the implementation exposes, and what checks it:

| Panel | Reports |
|---|---|
| `capability_catalog` | Implemented capabilities with route and symbol counts |
| `traceability_matrix` | Per capability: implementing files, receipts, and what exercises it |
| `verification_gaps` | Capabilities no verifying file reaches |
| `consequences` | Implications composed from claim categories, never recommendations |

Declared surface — the names and shapes a repository writes down:

| Panel | Reports |
|---|---|
| `symbol_index` | Every extracted symbol, with a short form matching import spelling |
| `signatures` | Parameters, annotations, defaults, and return types as written |
| `model_fields` | Annotated class attributes: the data contract stated outright |
| `payload_shapes` | Literal keys of dictionaries a function returns |
| `object_keys` | Field names coined as object literal keys |
| `imported_names` | Which names each imported module actually contributes |
| `data_containers` | Module-level lookup tables with their sizes |

Values and behaviour — decisions written into the code:

| Panel | Reports |
|---|---|
| `tunable_index` | Named numeric constants, module-level and per instance |
| `string_constants` | Named string values the system compares against |
| `embedded_literals` | Numbers hardcoded inside function bodies, which nothing else indexes |
| `failure_surface` | Raises recorded inside route handler bodies |

Runtime reach — what the code depends on but does not own:

| Panel | Reports |
|---|---|
| `external_api_surface` | Platform and library API reached from outside the module |
| `external_calls` | Which function of each imported module is actually called |
| `external_origins` | Third-party hosts named in string literals, stylesheets and markup |
| `config_settings` | Compiler and build settings that decide what the toolchain accepts |

Consolidated views — the same determinations, arranged for one question:

| Panel | Reports |
|---|---|
| `security_matrix` | Every security control this profile checks, and what was found |
| `endpoint_catalog` | Each route with its handler's guards, refusals, and response fields |
| `data_flow` | Where data enters, rests, and leaves each module |
| `substitute_analysis` | What plays an absent concern's part, since the work happens regardless |
| `documented_values` | What the documentation asserts, beside what the code declares |

The exclusions panel is the one that matters most. A census that silently drops
files overstates its own coverage, so excluded content is counted and labelled —
and every percentage elsewhere in the document is explicitly relative to the
included set.

Adding a panel means a function in `spec/panels.py`, an entry in `PANEL_KINDS`,
and a row here. The profile schema rejects a panel name it does not know, so a
typo fails at load rather than rendering an empty section.

## Capabilities and traceability

Sections §2.4–2.6 answer the question a specification exists to answer: *what does
this system do, what implements each piece, and what verifies it.*

**These are implemented capabilities, not requirements.** A requirement is a
statement of intent, and source code is not intent. Claiming to recover
requirements from an implementation would be inventing the part that matters
most, so the catalog says only what the code exposes.

Capabilities are clustered two ways, both structural:

- **Route groups** — verified `http_route` claims grouped by leading static path
  segment (`/action/attack/{id}` and `/action/move/{id}` become one capability).
- **Modules** — remaining source symbols grouped by containing package
  directory. Route handlers are excluded so nothing is counted twice.

### Traceability is computed, not asserted

Each capability is linked to what exercises it by following two edge kinds out of
**verifying files** — those the scanner assigned the `test` role, plus any file
cited by an `operator_harness` claim:

| Signal | Meaning |
|---|---|
| `calls` | A verifying file calls one of the capability's symbols |
| `references_route_path` | A verifying file contains a string literal naming one of its routes |

The second signal matters more than it sounds. A harness usually exercises an API
over HTTP rather than by importing handlers, so call edges alone report almost
everything as untested. Route-path literals close that gap. Both sides are reduced
to their static prefix before comparison, because a client builds
`/action/attack/{player_id}` with an f-string and the recorded literal is only
`/action/attack/`.

Two deliberate exclusions keep the number honest:

- A verifying file calling a helper **defined in that same file** is
  self-reference, not coverage, and is dropped.
- Calls from non-verifying files never count, however numerous.

### What a missing reference does and does not mean

`no-verifying-reference` means no call edge and no route-path literal was found
from any verifying file. Static resolution cannot observe dynamic dispatch,
reflection, or an end-to-end exercise that names an endpoint indirectly, so a row
in §2.6 is a **candidate for review, not proof that the capability is untested**.
The section says so in the rendered output.

## Citation integrity

`--verify` re-resolves every citation in the rendered document against the ledger
and against the current bytes on disk:

| Status | Meaning |
|---|---|
| `current` | Receipt resolves and the file hash still matches the snapshot |
| `source-changed` | The cited file was edited after the snapshot |
| `file-missing` | The cited file no longer exists |
| `unresolvable` | The receipt is not in the ledger, or its path escapes the root |
| `virtual` | A repository-wide census receipt with no single file |

Integrity is `(current + virtual) / total`. The command exits non-zero when any
citation fails, which makes it usable as a CI gate: a specification that cites
lines nobody can find is worse than no specification.

## Diagrams

Diagrams are generated only from structured records — edges, symbols, and counted
file facts. When the underlying data is absent, the generator emits a stated reason
rather than an invented picture. Truncation is always reported.

| Generator | Source | Output |
|---|---|---|
| `module_dependency` | `imports` edges resolved to internal module symbols | one flowchart |
| `route_surface` | Verified `http_route` claims grouped by path prefix | one flowchart |
| `concentration` | File line counts from the snapshot | one flowchart |
| `persistence_erd` | `storage_schema` and `storage_serialization` claims | one entity graph |
| `route_sequence` | Call edges scoped to each route handler symbol | one sequence diagram per route |

### Sequence diagrams

`route_sequence` emits one Mermaid sequence diagram per route, ordered by
interaction depth. Messages come from `calls` edges whose source symbol is the
route handler, sorted by the line number on their evidence receipt.

Two filters decide what appears, and both matter:

- **Decorator calls are excluded.** A call recorded above the handler's `def`
  line registers the route rather than participating in serving it.
- **Participants are resolved per file.** A call target such as `result.get`
  names a local value; `vec_db.save_player` names a module-owned object. Only
  module-level variables, classes, and imports **declared in the handler's own
  file** qualify. Resolving against every symbol in the repository lets a local
  named `player` collide with an unrelated module-level `player` elsewhere and
  fill the diagram with noise.

Repeated calls to the same collaborator method are drawn once.

### Handler guard and exit flows

`handler_flow` draws the decision structure a reader follows to understand when a
handler rejects a request: `if` guards as diamonds, `raise` as a rejection with
its HTTP status when the status is a literal, and each `return` as an exit. Every
node carries the line it came from.

It is a **guard-and-exit trace, not a control-flow graph**. Nested function and
class bodies are not entered because they run under a different call, loops
appear as a single node rather than unrolled, and nesting is bounded. A handler
with no guard and one exit is skipped: there is no decision to draw.

### Observed value assignments

`state_values` draws the string literals a field is assigned, and labels each
edge with the **actual enclosing condition** from source plus its line.

This is not a recovered state machine. Real code gates a state write on a
derived boolean — `if wiped: run.status = "wiped"` — so the guard is `wiped`,
not a comparison against the previous state. Drawing an edge from `active` to
`wiped` would require deciding that `wiped` implies the prior state, which the
source does not say. The diagram therefore shows where each value is set and
under what condition, and leaves the source state unasserted.

A field is drawn only when two or more distinct literals are assigned to it. A
field that is merely compared is not a state this module writes.

### What is deliberately not generated

**Reachability is never asserted.** No diagram here claims that a path can
execute, only that the source contains it. A guard behind a `cfg` gate, a branch
no caller reaches, and a live code path look identical to a reader of tokens.

## What this deliberately does not do

- It does not call a language model. The deterministic path produces the whole
  document.
- It does not resolve conflicts. Both sides of a contradiction survive into §9.3.
- It does not infer runtime behavior from static evidence, and says so in every
  document's interpretation boundary.
