# Adding a Language Analyzer

An analyzer turns files of one language into **symbols, relationships, evidence
receipts, claims, and a coverage record**. Everything downstream — the
specification, the diagrams, the capability catalog, the dashboard, the MCP
tools — is a projection of those five record types. Nothing needs to know your
language exists.

That is the whole extension contract. This document is what you need to add one.

## The contract

```python
class Analyzer(Protocol):
    name: str
    version: str

    def analyze(self, snapshot: Snapshot) -> AnalysisResult:
        """Analyze an immutable snapshot without executing target code."""
```

Register it in `analyze_snapshot` (`src/open_skeleton/analysis.py`):

```python
analyzers = (
    PythonAstAnalyzer(),
    TypeScriptLexicalAnalyzer(),
    RustLexicalAnalyzer(),
    YourAnalyzer(),          # <- here
    ProjectMetadataAnalyzer(),
)
```

Order matters only in that `ProjectMetadataAnalyzer` reconciles documentation
against what earlier analyzers found, so keep it last.

## Non-negotiables

These are enforced by the model and by review, not by convention:

1. **Never execute target code.** No import, no subprocess, no eval. Parse or
   tokenize only.
2. **Never reach the network.**
3. **Never write inside the analyzed repository.**
4. **Verify the file hash before reading.** The snapshot pinned it; if the bytes
   changed since, record a coverage failure rather than analyzing stale content:

   ```python
   payload = (snapshot.root / Path(record.path)).read_bytes()
   if hashlib.sha256(payload).hexdigest() != record.sha256:
       raise ValueError("content changed after snapshot")
   ```

5. **Every claim carries a receipt.** `ClaimRecord.__post_init__` rejects a
   `verified` claim with no supporting evidence. For a counted absence — "no
   test attribute appears anywhere" — emit a census receipt whose `path` is `"."`
   so per-file yield attribution correctly ignores it.
6. **Use `stable_id` for every identifier.** Two runs over identical bytes must
   produce identical output; the test suite asserts this.

## What to emit

| Record | Emit one when |
|---|---|
| `SymbolRecord` | A named thing exists: module, function, class, type |
| `EdgeRecord` | Two things relate: `imports`, `calls`, `contains`, `declares_dependency` |
| `EvidenceRecord` | Any line or span you will cite |
| `ClaimRecord` | A statement about the code that a receipt supports |
| `CoverageRecord` | Once, summarizing eligible / analyzed / failed files |

Reuse existing edge relationships and claim categories where they fit. A new
category is fine when the concept is genuinely new — it just needs a home in a
spec profile section, or it lands in §9.4 as unrouted.

## Choose your accuracy tier, and say which

| Tier | Use when | Report coverage as |
|---|---|---|
| Native AST | The language ships a parser you can call from Python | exact |
| Lexical | You can tokenize safely but not parse | lexical |
| Native index | The toolchain emits a semantic graph you can consume | supplied index |
| Declared | The fact is stated outright in the source and needs no parser | declared |

`PythonAstAnalyzer` uses the stdlib `ast` module — the reference parser, so its
facts are exact. `RustLexicalAnalyzer` and `TypeScriptLexicalAnalyzer` tokenize.
`HumSemanticIndexAnalyzer` consumes `hum.semantic_graph.v0` and **never runs the
compiler**; without a supplied index it reports zero coverage and says so.
`SqlSchemaAnalyzer` is `declared`: DDL states the schema rather than implying
it, so the columns and keys it reports are exact even though the file holding
them was never parsed as a program.

Prefer a native index over a lexical guess when the toolchain offers one. Prefer
saying "lexical" over implying more.

### If you tokenize, handle the traps

Every language has a few, and getting them wrong silently corrupts every count
downstream. `rust_lexical.py` documents its three at the top of the module:
nested block comments, hashed raw strings, and lifetimes that look like character
literals. Find your language's equivalents before writing the first claim, and
give each one a test.

## Claim yield: the honesty check

Coverage means "the file parsed". **Yield** means "the file produced a finding".
An analyzer that parses everything and claims nothing reports 100% coverage and
near-zero yield, and the specification prints that gap in its executive summary.

If your analyzer lands with low yield, that is not a failure — it is the report
telling the truth about a thin read. Do not pad it with claims that say nothing.

Yield per repository is not the check, because it conflates a weak analyzer with
a clean codebase — a crate with no global mutable state and an analyzer that
cannot find one score the same. Run the generalization benchmark, which measures
each analyzer against the files it actually read:

```powershell
python benchmarks\generalization\run_generalization.py `
  --repo C:\path\to\one --repo C:\path\to\two --output-dir generalization-output
```

The Rust analyzer sat at 0.13 claims per file it read while Python sat at 1.83.
That gap was invisible to every pinned benchmark in this repository, because a
pinned benchmark measures one fixture and cannot see a language it does not
contain.

## Reuse a claim category before inventing one

A module-scope container written to at runtime is `process_local_state` in
Python, TypeScript and Rust. It could have been `ui_state` in one and
`shared_static` in another, and then a reader would need three vocabularies for
one fact and no cross-language consequence rule could ever fire.

Before adding a category, look for the one that already names your finding.
`grep -rn 'category=' src/open_skeleton/analyzers/` is the whole search. The
generalization report lists every category produced for exactly one repository,
which is where a needlessly novel name shows up.

## Panels must tolerate what your analyzer omits

A panel reads metadata your analyzer wrote and must not assume every key is
present. A Rust `static` can be declared in one place and assigned in another,
so its constant entry has a name and a line and no value — and an early version
of `tunable_index` indexed `entry["value"]` directly and took down the whole
document on the first Rust repository it met.

If you add a panel, read optional fields with `.get` and a sensible default.
Crashing a specification over one missing key is the wrong contract to offer a
contributed analyzer, and contributed analyzers are the point.

## Tests

`CONTRIBUTING.md` requires one deterministic positive test and one negative or
adversarial test per behavior. For an analyzer the adversarial cases that matter:

- a keyword inside a **string literal** must not be counted
- a keyword inside a **comment** must not be counted
- output must be **identical across two runs**
- a file whose bytes changed after the snapshot must be a **coverage failure**
- a construct that merely resembles the one you detect must be **excluded**

`tests/test_rust_analyzer.py` is the model to copy.

## Worked reference

`src/open_skeleton/analyzers/rust_lexical.py` is roughly 400 lines and was added
in one commit. It emits module and item symbols, `imports` edges, and four claim
categories: `unsafe_surface`, `panic_site`, `testing`, `application_entry`.

Its value came from asking one question first: *what does a reviewer of this
language ask before anything else?* For Rust that is where `unsafe` appears and
where the code can panic — not a generic symbol dump. Answer that question for
your language and the claims write themselves.

## Wiring it into the report

Nothing is required. Claims land in §9.4 unrouted until a profile section
selects them. To give them a home, add a node to
`src/open_skeleton/spec/profiles/standard.json`:

```json
{
  "id": "security.memory-safety",
  "number": "6.6",
  "title": "Memory-Safety Escape Hatches",
  "probes": [
    {"name": "Unsafe surface", "kind": "sourced_claim_category", "terms": ["unsafe_surface"]}
  ],
  "findings": {"categories": ["unsafe_surface"], "limit": 15}
}
```

Use `sourced_claim_category` for any category that can report a counted absence,
so a claim saying "none of these exist" is not read as proof they do. See
[SPEC.md](SPEC.md).
