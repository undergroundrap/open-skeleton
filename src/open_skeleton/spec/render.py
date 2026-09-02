# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from open_skeleton.ledger import EvidenceLedger
from open_skeleton.models import utc_now
from open_skeleton.spec.capabilities import Capability, build_capabilities
from open_skeleton.spec.concordance import (
    ContractRecord,
    ContractRoute,
    ContractValueSet,
    build_contract_concordance,
    build_record_concordance,
    build_value_set_concordance,
)
from open_skeleton.spec.consequences import Consequence, derive
from open_skeleton.spec.diagrams import Diagram, build_diagrams
from open_skeleton.spec.dossiers import Dossier, build_dossiers, render_dossiers
from open_skeleton.spec.panels import Panel, PanelContext, build_panel, short_form
from open_skeleton.spec.probes import LedgerCorpus, ProbeResult, evaluate_section, run_probe
from open_skeleton.spec.profile import VERDICTS, SpecProfile, SpecSection, SpecSelector
from open_skeleton.spec.roles import MultiRole, derive_roles
from open_skeleton.spec.substitutes import Substitute, derive_substitutes

SPEC_SCHEMA_VERSION = "open-skeleton.spec.v1"
SPEC_INDEX_SCHEMA_VERSION = "open-skeleton.spec_index.v1"
# A section resting on a whole package would otherwise print a paragraph of
# paths. The complete list stays in the JSON projection.
MAX_EXAMINED_FILES = 12
# Above this share, claims reaching no section stop being an oddity and start
# being a statement about the profile. Six extractors were once added here
# without routing any of them, and seventy-eight claims sat in the catch-all
# unnoticed because nothing counted them where a reader would look.
UNMAPPED_ATTENTION_SHARE = 0.05
# `docs/SPEC.md` states why the symbol inventory was split out of the JSON:
# the inventories "scale with the repository rather than with what is
# interesting in it". That reasoning was applied to one output and not the
# other, and a 942-row table was 27% of a Rust specification. The rows stay
# complete in `spec.json`; the document shows enough to read and says where
# the rest is.
MAX_PANEL_ROWS = 25
# Probe kinds whose matches are things a person can read. The rest match on
# claim, symbol or edge identifiers, which are content digests: useful in
# the JSON projection for an agent resolving a reference, and twelve lines
# of unreadable hexadecimal in a document meant for a human.
LEGIBLE_PROBE_KINDS = frozenset(
    {"path_glob", "file_language", "file_role", "dependency_name", "import_target"}
)

_VERDICT_SENTENCE = {
    "applicable": (
        "**Determination: present.** Probes for this concern matched {total:,} "
        "record(s) in snapshot `{snapshot}`."
    ),
    "degenerate": (
        "**Determination: present in a reduced form.** Probes matched {total:,} "
        "record(s), below the {threshold:,} this profile treats as a full "
        "implementation of the concern."
    ),
    "absent": (
        "**Determination: absent.** Every probe declared for this concern returned "
        "zero matches across the {corpus:,} file(s) and {claims:,} claim(s) in snapshot "
        "`{snapshot}`. The queries are listed below so the absence can be re-checked "
        "rather than trusted."
    ),
    "structural": (
        "This section organizes the subsections below and makes no presence claim of its own."
    ),
    "evidenced": (
        "**Determination: present, but not by probe.** Every probe declared for this "
        "concern returned zero matches, and {findings:,} claim(s) about it were still "
        "selected into this section. The probes are listed below: a query that misses "
        "what the findings show is a gap in this profile, not in the repository."
    ),
    "not_applicable": (
        "**Determination: not applicable.** This concern presupposes {requires}, which "
        "this repository does not have, so its absence here is not a gap. The probes "
        "ran and are listed below, because a reader who disagrees with the "
        "precondition should be able to see what they would have matched."
    ),
}


@dataclass(frozen=True, slots=True)
class Citation:
    """A claim rendered with the receipt that backs it."""

    evidence_id: str
    path: str
    start_line: int | None
    end_line: int | None
    file_sha256: str | None
    relationship: str

    @property
    def location(self) -> str:
        if self.path == "." or self.start_line is None:
            return self.path
        if self.end_line and self.end_line != self.start_line:
            return f"{self.path}:{self.start_line}-{self.end_line}"
        return f"{self.path}:{self.start_line}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "file_sha256": self.file_sha256,
            "relationship": self.relationship,
            "location": self.location,
        }


@dataclass(frozen=True, slots=True)
class RenderedClaim:
    claim_id: str
    claim: str
    category: str
    status: str
    confidence: float
    importance: str
    citations: tuple[Citation, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claim": self.claim,
            "category": self.category,
            "status": self.status,
            "confidence": self.confidence,
            "importance": self.importance,
            "citations": [item.to_dict() for item in self.citations],
        }


@dataclass(frozen=True, slots=True)
class RenderedSection:
    section_id: str
    number: str
    title: str
    concern: str
    framing: str
    verdict: str
    probe_results: tuple[ProbeResult, ...]
    findings: tuple[RenderedClaim, ...]
    constraints: tuple[RenderedClaim, ...]
    diagrams: tuple[Diagram, ...]
    panels: tuple[Panel, ...]
    cross_references: tuple[str, ...]
    omitted_findings: int
    depth: int
    omitted_claim_ids: tuple[str, ...] = ()
    degenerate_threshold: int = 0
    examined_files: tuple[tuple[str, int], ...] = ()
    unmet_requirements: tuple[str, ...] = ()
    candidate_results: tuple[ProbeResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "number": self.number,
            "title": self.title,
            "concern": self.concern,
            "verdict": self.verdict,
            "degenerate_threshold": self.degenerate_threshold,
            "probes": [item.to_dict() for item in self.probe_results],
            "findings": [item.to_dict() for item in self.findings],
            "constraints": [item.to_dict() for item in self.constraints],
            "diagrams": [item.to_dict() for item in self.diagrams],
            "panels": [item.to_dict() for item in self.panels],
            "cross_references": list(self.cross_references),
            "omitted_findings": self.omitted_findings,
            "omitted_claim_ids": list(self.omitted_claim_ids),
            "depth": self.depth,
            "examined_files": [
                {"path": path, "receipts": count} for path, count in self.examined_files
            ],
            "unmet_requirements": list(self.unmet_requirements),
            "candidates": [item.to_dict() for item in self.candidate_results],
        }


@dataclass(frozen=True, slots=True)
class SpecDocument:
    """A complete specification as data. Markdown is one projection of this."""

    schema: str
    snapshot_id: str
    root: str
    profile_id: str
    profile_title: str
    profile_lineage: str
    generated_at: str
    sections: tuple[RenderedSection, ...]
    coverage: tuple[dict[str, Any], ...]
    stale_claim_count: int
    total_claims: int
    cited_claims: int
    capabilities: tuple[Capability, ...] = ()
    contract_concordance: tuple[ContractRoute, ...] = ()
    # Vocabularies written out in more than one form. A reader adding a
    # member has to change every one of them, and nothing else says where
    # they are.
    value_set_concordance: tuple[ContractValueSet, ...] = ()
    ambiguous_value_labels: tuple[str, ...] = ()
    # Record shapes stated as a table, a class, and a schema at once.
    record_concordance: tuple[ContractRecord, ...] = ()
    consequences: tuple[Consequence, ...] = ()
    dossiers: tuple[Dossier, ...] = ()
    substitutes: tuple[Substitute, ...] = ()
    roles: tuple[MultiRole, ...] = ()
    symbols: tuple[dict[str, Any], ...] = ()
    name_index: dict[str, dict[str, int]] = field(default_factory=dict)
    # How many rows of each kind this document was built from. Recorded so
    # the totals can be reconciled against the ledger: edges never reach
    # the rendered document, so without this there is nothing to compare
    # and a truncated graph is undetectable after the fact.
    source_counts: dict[str, int] = field(default_factory=dict)
    # Languages the census holds that no analyzer produced any record for.
    # Distinct from a parse failure: nothing here was ever attempted.
    unread_languages: tuple[tuple[str, int], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "capabilities": [item.to_dict() for item in self.capabilities],
            "contract_concordance": [item.to_dict() for item in self.contract_concordance],
            "value_set_concordance": [item.to_dict() for item in self.value_set_concordance],
            "ambiguous_value_labels": list(self.ambiguous_value_labels),
            "record_concordance": [item.to_dict() for item in self.record_concordance],
            "consequences": [item.to_dict() for item in self.consequences],
            "dossiers": [item.to_dict() for item in self.dossiers],
            "substitutes": [item.to_dict() for item in self.substitutes],
            "multi_role_structures": [item.to_dict() for item in self.roles],
            "snapshot_id": self.snapshot_id,
            "root": self.root,
            "profile_id": self.profile_id,
            "profile_title": self.profile_title,
            "profile_lineage": self.profile_lineage,
            "generated_at": self.generated_at,
            "coverage": [dict(item) for item in self.coverage],
            "stale_claim_count": self.stale_claim_count,
            "total_claims": self.total_claims,
            "cited_claims": self.cited_claims,
            "sections": [item.to_dict() for item in self.sections],
        }

    def index_to_dict(self) -> dict[str, Any]:
        """The two complete inventories, kept out of the document itself.

        Both are untruncated on purpose and both scale with the repository
        rather than with what is interesting in it: on a 523-file tree they
        were 37% of a six-megabyte file that a consumer had to parse in full
        to read a section. Splitting them means an agent that wants every name
        still gets it, and one that wants the document is not charged for it.
        """

        return {
            "schema": SPEC_INDEX_SCHEMA_VERSION,
            "snapshot_id": self.snapshot_id,
            "generated_at": self.generated_at,
            # Complete and untruncated. The markdown index is a readable
            # selection; a consumer needing every name the analyzers found
            # reads this instead of re-deriving it from the repository.
            "symbols": [dict(item) for item in self.symbols],
            # A concordance, not analysis: every name each file mentions and
            # the line it first appears on.
            "name_index": {path: dict(names) for path, names in sorted(self.name_index.items())},
        }


def _by_category(claims: Iterable[dict[str, Any]]) -> dict[str, tuple[dict[str, Any], ...]]:
    """Group claims by category so a panel can consolidate across sections."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        grouped.setdefault(str(claim.get("category", "")), []).append(claim)
    return {name: tuple(items) for name, items in grouped.items()}


def _examined_files(
    findings: tuple[RenderedClaim, ...],
    constraints: tuple[RenderedClaim, ...],
) -> tuple[tuple[str, int], ...]:
    """Which files this section's conclusions were actually read out of.

    Every claim already carries receipts and every receipt already carries a
    path, so this asserts nothing new. What it adds is the reverse lookup a
    reviewer wants: not "where did this sentence come from" but "which files
    did you have to read to write this section", which is the question asked
    when deciding whether a section can be trusted or needs checking.

    A repository-wide census receipt has no file behind it and is excluded, so
    a section resting only on those correctly reports nothing examined rather
    than claiming the whole tree.
    """

    counts: dict[str, int] = {}
    for claim in (*findings, *constraints):
        for citation in claim.citations:
            path = citation.path
            if path in {".", ""} or path.startswith("@"):
                continue
            counts[path] = counts.get(path, 0) + 1
    return tuple(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def _select(
    selector: SpecSelector | None,
    claims: Iterable[dict[str, Any]],
    used: set[str],
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Claims this selector takes, how many it could not print, and every id it claimed.

    The third return value exists because a claim past the limit has still been
    routed. Marking only the printed ones as taken left the remainder
    unclaimed, so they fell through to the catch-all and were reported under
    "Claims Not Mapped to an Outline Section" -- which was false. They were
    mapped; the section simply declined to print them, and says so in the
    sentence directly above.
    """

    if selector is None:
        return [], 0, []
    eligible = [
        claim for claim in claims if selector.accepts(claim) and claim["claim_id"] not in used
    ]
    return (
        eligible[: selector.limit],
        max(0, len(eligible) - selector.limit),
        [str(item["claim_id"]) for item in eligible],
    )


def _citations(
    claim: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]
) -> tuple[Citation, ...]:
    citations: list[Citation] = []
    pairs = [(evidence_id, "supports") for evidence_id in claim.get("supporting_evidence", ())] + [
        (evidence_id, "contradicts") for evidence_id in claim.get("contradicting_evidence", ())
    ]
    for evidence_id, relationship in pairs:
        record = evidence_by_id.get(evidence_id)
        if record is None:
            continue
        citations.append(
            Citation(
                evidence_id=evidence_id,
                path=str(record["path"]),
                start_line=record["start_line"],
                end_line=record["end_line"],
                file_sha256=record.get("file_sha256"),
                relationship=relationship,
            )
        )
    return tuple(citations)


CLAIM_PAGE = 5_000
EDGE_PAGE = 20_000


def every_claim(ledger: EvidenceLedger, snapshot_id: str) -> list[dict[str, Any]]:
    """All claims for a snapshot, not the first page of them.

    The ledger caps a single query at 5,000 because the interactive commands
    that share it should not stream a whole repository into a terminal. The
    specification builder asked for exactly that cap and reported what came
    back as the ledger's total, so a snapshot with more claims lost the
    remainder without saying so: `java.base` produces 8,707 and the document
    announced "Claims in ledger: 5,000".

    Nothing smaller than a real repository reached the limit, which is why it
    survived every corpus until a language with 3,000 files in one module.
    """

    found: list[dict[str, Any]] = []
    while True:
        page = ledger.list_claims(snapshot_id, limit=CLAIM_PAGE, offset=len(found))
        found.extend(page)
        if len(page) < CLAIM_PAGE:
            return found


def _every_edge(ledger: EvidenceLedger, snapshot_id: str) -> list[dict[str, Any]]:
    """All relationship edges, for the same reason as `every_claim`.

    The default page here is 20,000 and `java.base` holds 21,865, so the
    graph the document reasoned over was quietly missing 1,865 relationships.
    Capability traceability is computed from these edges, which means the
    truncation did not merely omit rows: it could report a capability as
    reached by no test because the call edge proving otherwise fell off the
    end of a page.
    """

    found: list[dict[str, Any]] = []
    while True:
        page = ledger.list_edges(snapshot_id, limit=EDGE_PAGE, offset=len(found))
        found.extend(page)
        if len(page) < EDGE_PAGE:
            return found


def _every_symbol(ledger: EvidenceLedger, snapshot_id: str) -> list[dict[str, Any]]:
    """All symbols for a snapshot, for the same reason as `every_claim`.

    The index projection exists to carry the complete inventory that the
    document deliberately leaves out, so truncating it at one page defeats
    the split: `java.base` declares 5,638 symbols and the index held 5,000.
    """

    found: list[dict[str, Any]] = []
    while True:
        page = ledger.list_symbols(snapshot_id, limit=CLAIM_PAGE, offset=len(found))
        found.extend(page)
        if len(page) < CLAIM_PAGE:
            return found


def build_spec(
    ledger: EvidenceLedger,
    profile: SpecProfile,
    *,
    snapshot_id: str | None = None,
) -> SpecDocument:
    """Project the ledger of one snapshot through an outline profile."""

    latest = ledger.latest_snapshot()
    resolved_id = snapshot_id or (str(latest["snapshot_id"]) if latest else None)
    if resolved_id is None:
        raise ValueError("No snapshot exists; run `open-skeleton analyze` first")
    snapshot_row: dict[str, Any] = dict(latest) if latest else {}
    root = str(snapshot_row.get("root_path", ""))

    files = tuple(ledger.list_files(resolved_id))
    exclusions = tuple(ledger.list_exclusions(resolved_id))
    claims = tuple(every_claim(ledger, resolved_id))
    symbols = tuple(_every_symbol(ledger, resolved_id))
    edges = tuple(_every_edge(ledger, resolved_id))
    evidence_by_id = {str(item["evidence_id"]): item for item in ledger.list_evidence(resolved_id)}
    corpus = LedgerCorpus(
        snapshot_id=resolved_id,
        files=files,
        claims=claims,
        symbols=symbols,
        edges=edges,
        evidence=tuple(evidence_by_id.values()),
    )

    capabilities = build_capabilities(
        files=files,
        claims=claims,
        symbols=symbols,
        edges=edges,
        evidence_by_id=evidence_by_id,
    )
    contract_concordance = build_contract_concordance(
        snapshot_id=resolved_id,
        claims=claims,
        evidence_by_id=evidence_by_id,
    )
    value_set_concordance, ambiguous_value_labels = build_value_set_concordance(
        snapshot_id=resolved_id,
        symbols=symbols,
    )
    record_concordance = build_record_concordance(
        snapshot_id=resolved_id,
        symbols=symbols,
    )
    # Consequences need the absent verdicts, which are only known once every
    # section has been evaluated, so panels are rebuilt after that pass below.
    panel_context = PanelContext(
        files=files,
        exclusions=exclusions,
        snapshot=snapshot_row,
        capabilities=capabilities,
        contract_concordance=contract_concordance,
        value_set_concordance=value_set_concordance,
        record_concordance=record_concordance,
        symbols=symbols,
    )

    used: set[str] = set()
    # A selector with no category filter takes whatever is left, so it must not
    # run until every filtered selector has had its turn. The catch-all sat at
    # §9.4 and quietly took the claims §9.6 was written to hold, which read as
    # "not mapped to an outline section" for a family that had a section two
    # entries later. Claiming in this order makes the outline's own ordering
    # irrelevant to which section a fact lands in.
    for candidate in profile.walk():
        selector = candidate.findings
        if selector is None or not selector.categories:
            continue
        taken, _, routed = _select(selector, claims, used)
        del taken
        used.update(routed)
    filtered_claims = set(used)
    used = set()
    # Verdicts as they are decided, so a section can read what it presupposes.
    decided: dict[str, str] = {}
    rendered: list[RenderedSection] = []

    def render_node(section: SpecSection, depth: int) -> None:
        verdict, probe_results = evaluate_section(section, corpus)
        selector = section.findings
        # A catch-all sees only what no filtered section claimed; a filtered
        # section competes normally with its peers in document order.
        taken_before = (
            used | filtered_claims if selector is not None and not selector.categories else used
        )
        selected, omitted, routed = _select(selector, claims, taken_before)
        used.update(routed)
        constraint_claims, _, _ = _select(section.constraints, claims, set())

        # Selection has to happen before the verdict is final. A probe that
        # matches nothing while claims about the concern land in this very
        # section has not shown the concern is absent -- it has shown the
        # probe is the wrong query. Runtime Topology read "absent: every probe
        # returned zero matches" directly above seven verified findings, and
        # the executive summary counted it among concerns the repository does
        # not implement while the document itself showed otherwise.
        if verdict == "absent" and selected:
            verdict = "evidenced"
        findings = tuple(
            RenderedClaim(
                claim_id=str(item["claim_id"]),
                claim=str(item["claim"]),
                category=str(item["category"]),
                status=str(item["status"]),
                confidence=float(item["confidence"]),
                importance=str(item["importance"]),
                citations=_citations(item, evidence_by_id),
            )
            for item in selected
        )
        constraints = tuple(
            RenderedClaim(
                claim_id=str(item["claim_id"]),
                claim=str(item["claim"]),
                category=str(item["category"]),
                status=str(item["status"]),
                confidence=float(item["confidence"]),
                importance=str(item["importance"]),
                citations=_citations(item, evidence_by_id),
            )
            for item in constraint_claims
        )
        diagrams = tuple(
            diagram
            for name in section.diagrams
            for diagram in build_diagrams(
                name,
                files=files,
                claims=claims,
                symbols=symbols,
                edges=edges,
                evidence_by_id=evidence_by_id,
            )
        )
        panels = tuple(build_panel(name, panel_context) for name in section.panels)

        # Diagrams and panels are projections of the same pinned ledger as the
        # findings. If one of them renders concrete rows or edges, the concern
        # cannot remain absent merely because its configured probe missed the
        # underlying representation. Architectural Concentration once printed
        # a populated concentration graph under an ``absent`` verdict; the
        # graph was true and the probe was incomplete. Reconcile all output
        # shapes before dependent sections read this verdict.
        if verdict == "absent" and (
            any(diagram.mermaid for diagram in diagrams) or any(panel.rows for panel in panels)
        ):
            verdict = "evidenced"

        # Adjacent queries are run only when the concern was not found. If a
        # finding, diagram, or panel established it, what is nearby is not the
        # interesting fact.
        candidate_results = (
            tuple(run_probe(probe, corpus) for probe in section.candidates)
            if verdict == "absent" and section.candidates
            else ()
        )
        # A concern can presuppose another. Pagination without an HTTP surface
        # is not a gap in the system, it is a question that does not arise —
        # and reporting fifty of those buries the handful that do. The
        # requirement is read from verdicts already decided this pass.
        unmet = [
            name
            for name in section.requires
            if decided.get(name) not in {"applicable", "degenerate", "evidenced"}
        ]
        if unmet and verdict == "absent":
            verdict = "not_applicable"
        decided[section.section_id] = verdict
        unmet_requirements = tuple(unmet) if verdict == "not_applicable" else ()

        rendered.append(
            RenderedSection(
                section_id=section.section_id,
                number=section.number,
                title=section.title,
                concern=section.concern,
                framing=section.framing,
                verdict=verdict,
                probe_results=probe_results,
                candidate_results=candidate_results,
                findings=findings,
                constraints=constraints,
                diagrams=diagrams,
                panels=panels,
                cross_references=section.cross_references,
                omitted_findings=omitted,
                depth=depth,
                omitted_claim_ids=tuple(
                    item for item in routed if item not in {c.claim_id for c in findings}
                ),
                degenerate_threshold=section.degenerate_below,
                examined_files=_examined_files(findings, constraints),
                unmet_requirements=unmet_requirements,
            )
        )
        for child in section.children:
            render_node(child, depth + 1)

    for section in profile.sections:
        render_node(section, 0)

    absent = frozenset(
        term
        for item in rendered
        if item.verdict == "absent"
        for probe in item.probe_results
        if probe.kind in {"claim_category", "sourced_claim_category"}
        for term in probe.query.split(": ", 1)[-1].split(", ")
    )
    claim_locations: dict[str, str] = {}
    for claim in claims:
        for evidence_id in claim.get("supporting_evidence", ()):
            record = evidence_by_id.get(evidence_id)
            if record is None or str(record["path"]) in {".", ""}:
                continue
            line = record["start_line"]
            claim_locations[str(claim["claim_id"])] = (
                f"{record['path']}:{line}" if line else str(record["path"])
            )
            break
    consequences = derive(claims, absent_categories=absent)
    roles = derive_roles(tuple(claims), tuple(evidence_by_id.values()))
    substitutes = derive_substitutes(
        symbols,
        tuple(claims),
        absent_sections=frozenset(item.section_id for item in rendered if item.verdict == "absent"),
    )
    dossiers = build_dossiers(capabilities, claims, evidence_by_id, consequences)
    panel_context = replace(
        panel_context,
        consequences=consequences,
        claim_locations=claim_locations,
        substitutes=substitutes,
        roles=roles,
        section_verdicts={item.section_id: item.verdict for item in rendered},
        claims_by_category=_by_category(claims),
    )
    for index, item in enumerate(rendered):
        if not item.panels:
            continue
        rendered[index] = replace(
            item,
            panels=tuple(build_panel(panel.name, panel_context) for panel in item.panels),
        )

    cited = sum(1 for item in rendered for claim in item.findings if claim.citations)
    return SpecDocument(
        schema=SPEC_SCHEMA_VERSION,
        snapshot_id=resolved_id,
        root=root,
        profile_id=profile.qualified_id,
        profile_title=profile.title,
        profile_lineage=profile.lineage,
        generated_at=utc_now(),
        sections=tuple(rendered),
        coverage=tuple(ledger.analysis_coverage(resolved_id)),
        stale_claim_count=len(ledger.stale_claims(resolved_id)),
        total_claims=len(claims),
        cited_claims=cited,
        source_counts={
            "claims": len(claims),
            "symbols": len(symbols),
            "edges": len(edges),
            # Recorded so the summary can tell "this repository has no tests"
            # apart from "its tests reach the code in a way tracing cannot
            # follow". A `testing` claim was the earlier proxy and it is the
            # wrong one: coast-most carries an 1,101-line self-test that
            # declares no recognized test block, so it produced no such claim
            # while plainly being a suite.
            "test_files": sum(1 for item in files if str(item["role"]) == "test"),
        },
        capabilities=capabilities,
        contract_concordance=contract_concordance,
        value_set_concordance=value_set_concordance,
        record_concordance=record_concordance,
        ambiguous_value_labels=ambiguous_value_labels,
        consequences=consequences,
        dossiers=dossiers,
        substitutes=substitutes,
        roles=roles,
        unread_languages=_languages_no_analyzer_read(files, symbols, evidence_by_id.values()),
        name_index={
            str(item["path"]): dict(item["metadata"]["name_index"])
            for item in symbols
            if isinstance(item.get("metadata"), dict) and item["metadata"].get("name_index")
        },
        symbols=tuple(
            {
                "qualified_name": str(item["qualified_name"]),
                "short_form": short_form(str(item["qualified_name"])),
                "kind": str(item["kind"]),
                "path": str(item["path"]),
                "start_line": item["start_line"],
                "end_line": item["end_line"],
                "language": item.get("language"),
                "analyzer": item.get("analyzer"),
            }
            for item in symbols
        ),
    )


def _escape(value: str) -> str:
    return value.replace("|", "\\|")


MAX_RENDERED_CITATIONS = 6
MAX_SUMMARY_ROWS = 10
MAX_UNREAD_LANGUAGES = 4
# Enough to name the conventions a reader knows without printing a dictionary.
MAX_NAMED_ABSENCES = 8

# The coherence checker locates the absence tally by this heading. Both sides
# import it so that renaming the section cannot quietly disable the check that
# guards it -- a mismatch would leave the checker returning "no incoherence"
# for a paragraph it had stopped reading.
ABSENCE_HEADING = "### Concerns with no matching evidence"


def _absent_artifacts(results: Iterable[ProbeResult]) -> str:
    """Name what was looked for and not found, one artifact at a time.

    This said "`path_glob: Dockerfile, docker-compose.yml` also matched
    nothing", which asks a reader to parse a query language to learn that
    there is no Dockerfile. Naming each artifact in its own span is how the
    sentence reads as a fact rather than as a log line -- and a measurement of
    this document against a long-form baseline put its coverage of *absence*
    facts at 2.4%, because absence was reported in the vocabulary of the tool
    instead of the vocabulary of the repository.
    """

    # Only kinds whose terms name something a reader could go and look for. A
    # claim category does not "appear in a snapshot" the way a file does, and
    # listing `delivery_automation` beside `Dockerfile` in one sentence claims
    # they are the same sort of thing.
    named: list[str] = []
    for result in results:
        if result.kind not in LEGIBLE_PROBE_KINDS:
            continue
        for term in result.terms:
            if term not in named:
                named.append(term)
    if not named:
        # Nothing a reader could go and look for, so there is nothing to say
        # that the query table above does not already say. The caller omits
        # the sentence rather than printing a claim about the tool.
        return ""
    spans = [f"`{_escape(term)}`" for term in named[:MAX_NAMED_ABSENCES]]
    remainder = len(named) - len(spans)
    if remainder > 0:
        spans.append(f"the {remainder:,} other name(s) queried")
    listed = spans[0] if len(spans) == 1 else ", ".join(spans[:-1]) + f" or {spans[-1]}"
    # "None of ... appears" stays grammatical whether one name is listed or
    # nine, which the previous phrasing did not.
    return (
        f"None of {listed} appears anywhere in this snapshot, so the concern is "
        "absent from what a reader would look for as well as from what the "
        "analyzers report."
    )


def _languages_no_analyzer_read(
    files: Iterable[dict[str, Any]],
    symbols: Iterable[dict[str, Any]],
    evidence: Iterable[dict[str, Any]],
) -> tuple[tuple[str, int], ...]:
    """Languages present in the census that no analyzer produced a record for.

    `_unread_files` counts files an analyzer declared eligible and then failed
    to parse. A language with no analyzer at all never appears in a coverage
    record, so it was invisible -- and the document went on to say "every
    eligible file parsed, so these absences are bounded by what the analyzers
    can express, not by anything left unread". For a repository holding a
    shell script that sentence was false, and it is the exact confusion this
    document exists to prevent: an absence resting on an unread file reads
    identically to an absence that was checked.

    Touched is defined by output rather than by declared eligibility, because
    an analyzer's own account of what it was willing to read cannot show that
    nothing was willing to read a language.
    """

    touched = {str(item["path"]) for item in symbols}
    touched.update(str(item["path"]) for item in evidence if item.get("path"))
    unread: Counter[str] = Counter()
    for item in files:
        if str(item["path"]) not in touched:
            unread[str(item["language"])] += 1
    covered = {str(item["language"]) for item in files if str(item["path"]) in touched}
    return tuple(
        sorted(
            ((language, count) for language, count in unread.items() if language not in covered),
            key=lambda pair: (-pair[1], pair[0]),
        )
    )


def _unread_files(coverage: Iterable[dict[str, Any]]) -> tuple[int, tuple[str, ...]]:
    """Eligible files that no analyzer parsed, and the languages they sit in.

    An absence is only as strong as the read it rests on. "This repository does
    not implement authentication" and "the files that would have shown it did
    not parse" produce the identical probe result -- zero matches -- and the
    difference between them is the entire question a reader is asking. The
    counts already exist per analyzer, so this re-reads nothing.
    """

    total = 0
    languages: list[str] = []
    for item in coverage:
        missed = int(item["eligible_files"]) - int(item["analyzed_files"])
        if missed <= 0:
            continue
        total += missed
        language = str(item["language"])
        if language not in languages:
            languages.append(language)
    return total, tuple(sorted(languages))


def _spread_by_category(claims: list[RenderedClaim], limit: int) -> list[RenderedClaim]:
    """The most important findings, one category at a time.

    Taking the first N by importance means the most *numerous* high-importance
    category fills the summary. On this repository that produced a headline
    reading "this file is long" six times out of ten while the storage schema,
    the entry points, and the migration behaviour never appeared.

    Ranking is unchanged; only the order of presentation is. Categories are
    visited round-robin so the first row of each is seen before the second row
    of any, which surfaces breadth without inventing a new notion of severity.
    """

    grouped: dict[str, list[RenderedClaim]] = {}
    for claim in claims:
        grouped.setdefault(claim.category, []).append(claim)
    spread: list[RenderedClaim] = []
    depth = 0
    while len(spread) < limit:
        added = False
        for bucket in grouped.values():
            if depth < len(bucket):
                spread.append(bucket[depth])
                added = True
                if len(spread) == limit:
                    return spread
        if not added:
            break
        depth += 1
    return spread


THIN_YIELD_RATIO = 0.34


# A reader who has to open a file to see what a claim rests on is doing the
# work the citation was supposed to save them. These bound how much source a
# document carries before it stops being a specification and becomes a copy.
MAX_EXCERPTS_PER_SECTION = 2
MAX_EXCERPT_LINES = 14
# A receipt spanning a whole file points at a file, not at a place. Showing
# its first few lines would print a licence header underneath a claim about
# something on line 900, so a span this long is cited and not quoted.
MAX_QUOTABLE_SPAN = 40
# Fence languages by suffix, so an excerpt highlights as what it is.
EXCERPT_LANGUAGES = {
    ".py": "python",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".json": "json",
    ".toml": "toml",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".md": "markdown",
    ".sql": "sql",
}


def _excerpt(root: Path, citation: Citation) -> tuple[str, ...] | None:
    """The source lines a citation names, or None when they cannot be trusted.

    Rendering bytes is only honest if they are still the bytes the receipt was
    taken from. A file that has changed since analysis would otherwise have its
    current contents printed underneath a claim about its former contents,
    which is a more convincing way to be wrong than printing nothing.

    The same containment check `verify_spec` uses applies here: a citation path
    that resolves outside the analyzed root is not read.
    """

    if citation.path == "." or citation.start_line is None or citation.file_sha256 is None:
        return None
    source = (root / Path(citation.path)).resolve()
    try:
        source.relative_to(root)
    except ValueError:
        return None
    if not source.is_file():
        return None
    span = (citation.end_line or citation.start_line) - citation.start_line
    if span > MAX_QUOTABLE_SPAN:
        return None
    payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != citation.file_sha256:
        return None
    lines = payload.decode("utf-8", errors="replace").splitlines()
    start = max(1, citation.start_line)
    end = min(len(lines), citation.end_line or citation.start_line)
    if start > len(lines) or end < start:
        return None
    return tuple(lines[start - 1 : min(end, start + MAX_EXCERPT_LINES - 1)])


def _excerpt_block(root: Path, claims: Iterable[RenderedClaim]) -> list[str]:
    """Rendered source for the first few claims in a section that can carry it."""

    rendered: list[str] = []
    shown = 0
    for claim in claims:
        if shown >= MAX_EXCERPTS_PER_SECTION:
            break
        for citation in claim.citations:
            lines = _excerpt(root, citation)
            if not lines:
                continue
            rendered.append(f"\n_{_escape(claim.claim)}_\n\n")
            suffix = "." + citation.path.rsplit(".", 1)[-1] if "." in citation.path else ""
            rendered.append(f"```{EXCERPT_LANGUAGES.get(suffix, 'text')}\n")
            rendered.extend(f"{line}\n" for line in lines)
            if (citation.end_line or 0) - (citation.start_line or 0) >= MAX_EXCERPT_LINES:
                rendered.append(f"# ... truncated at {MAX_EXCERPT_LINES} lines\n")
            rendered.append("```\n")
            rendered.append(f"\n<sub>{citation.location}</sub>\n")
            shown += 1
            break
    return rendered


def _first_citation(claim: RenderedClaim) -> str:
    if not claim.citations:
        return "—"
    return f"`{_escape(claim.citations[0].location)}`"


def _executive_summary(document: SpecDocument) -> list[str]:
    """Lead with what a reader has to decide, not with what the tool measured.

    A long specification is unusable if the reader has to find the important
    parts themselves. Everything here is selected from claims already rendered
    below, so the summary is a view rather than a second source of truth, and
    every row points at the section that carries the receipts.
    """

    section_of: dict[str, RenderedSection] = {}
    conflicts: list[RenderedClaim] = []
    urgent: list[RenderedClaim] = []
    for section in document.sections:
        for claim in section.findings:
            section_of[claim.claim_id] = section
            if claim.status == "conflict":
                conflicts.append(claim)
            elif claim.importance in {"critical", "high"} and claim.status != "unknown":
                urgent.append(claim)

    untraced = [item for item in document.capabilities if not item.exercised_by]
    absent = [item for item in document.sections if item.verdict == "absent"]
    probed = [item for item in document.sections if item.verdict != "structural"]
    thin = [
        item
        for item in document.coverage
        if int(item["analyzed_files"]) > 0
        and item.get("yield_ratio") is not None
        and float(item["yield_ratio"]) < THIN_YIELD_RATIO
    ]
    # An analyzer that read none of the files it was eligible for is the most
    # severe case of thin, and the condition above excludes it: a yield ratio
    # needs a denominator. A language project of 229 Hum files and 70 Rust ones
    # reported only that its Markdown was thin, while the analyzer for its main
    # language read nothing at all and said nothing about having done so.
    unread = [
        item
        for item in document.coverage
        if int(item["eligible_files"]) > 0 and int(item["analyzed_files"]) == 0
    ]

    lines = ["## Executive summary\n\n"]
    decisions = len(conflicts) + len(untraced)
    if decisions:
        lines.append(
            f"**{decisions} item(s) need a decision**: {len(conflicts):,} unresolved "
            f"conflict(s) between sources, and {len(untraced):,} implemented "
            "capability(ies) that no test or harness reaches.\n\n"
        )
    elif document.capabilities:
        lines.append(
            f"No unresolved conflicts, and all {len(document.capabilities):,} implemented "
            "capability(ies) are reached by a test or harness.\n\n"
        )
    else:
        # Saying every capability is covered when none was found is true and
        # reads as a pass. A repository of documentation earned that sentence
        # by containing no code, which is the opposite of what it announces.
        lines.append(
            "No unresolved conflicts. No implemented capability was clustered from this "
            "snapshot, so there is nothing here for a test to reach and nothing this "
            "line can tell you about coverage.\n\n"
        )

    if conflicts:
        lines.append("### Contradictions between sources\n\n")
        lines.append("| Finding | Evidence | Section |\n|---|---|---|\n")
        for claim in conflicts[:MAX_SUMMARY_ROWS]:
            location = section_of[claim.claim_id]
            lines.append(
                f"| {_escape(claim.claim)} | {_first_citation(claim)} | §{location.number} |\n"
            )
        if len(conflicts) > MAX_SUMMARY_ROWS:
            lines.append(f"\n_{len(conflicts) - MAX_SUMMARY_ROWS:,} further conflict(s) below._\n")
        lines.append("\n")

    if untraced:
        shown = untraced[:MAX_SUMMARY_ROWS]
        names = ", ".join(f"`{item.label}`" for item in shown)
        # The count and the list disagree once there are more than ten, and a
        # reader who cannot see that stops at the tenth name believing it is
        # the whole set. Every single-project run had fewer than ten, so the
        # truncation only became visible against a workspace of nine.
        if len(untraced) > len(shown):
            names += f", and {len(untraced) - len(shown):,} more"
        # A suite that drives a built artifact leaves no call edge into source,
        # so tracing sees nothing and the tally reads as an untested
        # repository. billune has an end-to-end test that renders the app and
        # asserts on its security headers; it imports `dist/server/index.js`,
        # names no source symbol, and every one of its capabilities was
        # reported as reached by nothing. Saying which of the two situations
        # this is costs a sentence, and is the difference between a fact and
        # an accusation.
        tested = bool(document.source_counts.get("test_files"))
        blind = (
            (
                " This repository does declare tests and none of them calls a capability by "
                "name: a suite that drives a built artifact or an HTTP surface reaches the "
                "code without leaving a call edge to follow, which is invisible to this "
                "tracing rather than absent from the repository."
            )
            if tested and len(untraced) == len(document.capabilities)
            else ""
        )
        lines.append(
            f"### Capabilities with no verifying reference\n\n"
            f"{len(untraced):,} of {len(document.capabilities):,}: {names}. "
            "Static resolution cannot see dynamic dispatch, so treat each as a "
            f"candidate for review rather than proof of absent testing.{blind}\n\n"
        )

    if urgent:
        lines.append("### Highest-importance verified findings\n\n")
        lines.append("| Finding | Evidence | Section |\n|---|---|---|\n")
        for claim in _spread_by_category(urgent, MAX_SUMMARY_ROWS):
            location = section_of[claim.claim_id]
            lines.append(
                f"| {_escape(claim.claim)} | {_first_citation(claim)} | §{location.number} |\n"
            )
        lines.append("\n")

    if absent:
        names = ", ".join(f"§{item.number} {item.title}" for item in absent[:MAX_SUMMARY_ROWS])
        remainder = len(absent) - MAX_SUMMARY_ROWS
        suffix = f", and {remainder:,} more" if remainder > 0 else ""
        missed, missed_languages = _unread_files(document.coverage)
        if missed:
            listed = ", ".join(missed_languages[:MAX_UNREAD_LANGUAGES])
            beyond = len(missed_languages) - MAX_UNREAD_LANGUAGES
            if beyond > 0:
                # Deliberately not "and N more": the enumeration checker reads
                # that idiom as the remainder of the *section* list in the same
                # paragraph, and would compare this count against that total.
                listed += f", plus {beyond:,} other language(s)"
            grounding = (
                # "did not parse" would overclaim: the shortfall counts files
                # that failed *and* files no analyzer was equipped to attempt,
                # and only the first of those was ever read.
                f" These hold only over what was read: {missed:,} eligible file(s) "
                f"({listed}) were not read, so evidence inside them was never "
                "available to be found."
            )
        else:
            grounding = (
                " Every eligible file parsed, so these absences are bounded by what the "
                "analyzers can express, not by anything left unread."
            )
        # A language an analyzer declared eligible and then did not parse is
        # already named above. Repeating it here said the same thing twice in
        # different words, which reads as two separate shortfalls.
        uncovered = [
            (language, count)
            for language, count in document.unread_languages
            if language not in set(missed_languages)
        ]
        if uncovered:
            named = ", ".join(f"{language} ({count:,})" for language, count in uncovered[:4])
            beyond = len(uncovered) - MAX_UNREAD_LANGUAGES
            if beyond > 0:
                named += f", plus {beyond:,} other language(s)"
            grounding += (
                f" Separately, no analyzer is equipped to read {named}, so an absence "
                "that would have been evidenced there could not have been found."
            )
        lines.append(
            f"{ABSENCE_HEADING}\n\n"
            f"{len(absent):,} of {len(probed):,} probed concerns returned no matches: "
            f"{names}{suffix}. Each prints the query that found nothing.{grounding}\n\n"
        )

    unmapped = next(
        (item for item in document.sections if item.section_id == "maintenance.unmapped"), None
    )
    if unmapped is not None and unmapped.findings and document.total_claims:
        stranded = len(unmapped.findings) + unmapped.omitted_findings
        share = stranded / document.total_claims
        if share >= UNMAPPED_ATTENTION_SHARE:
            families = ", ".join(
                sorted({_escape(claim.category) for claim in unmapped.findings})[:8]
            )
            lines.append("### Facts this outline has nowhere to put\n\n")
            lines.append(
                f"{stranded:,} of {document.total_claims:,} claims ({share:.0%}) matched no "
                f"section's selector and reach a reader only through §{unmapped.number}: "
                f"{families}. An analyzer that extracts a fact the profile never asks for "
                "produces a true statement nobody reads, so this is a gap in the outline "
                "rather than in the code it describes.\n\n"
            )

    if unread:
        lines.append("### Languages this analysis could not read\n\n")
        lines.append("| Language | Files eligible | Files read |\n|---|---:|---:|\n")
        for item in unread:
            lines.append(
                f"| {_escape(str(item['language']))} | {int(item['eligible_files']):,} | 0 |\n"
            )
        lines.append(
            "\nNothing below describes these files. An analyzer was eligible for them and "
            "produced no record, so every determination in this document was reached "
            "without reading them, and a concern implemented only there will read as "
            "absent.\n\n"
        )
        # The analyzer usually knows why it read nothing, and that reason is
        # often something the reader can act on -- the Hum analyzer asks for a
        # pre-generated index and names the flag that supplies it. Printing
        # the counts without the explanation turns an actionable gap into an
        # unexplained one, when the answer was already in the coverage record.
        for item in unread:
            for reason in tuple(item.get("failures", ()))[:2]:
                lines.append(f"_{_escape(str(item['language']))}: {_escape(str(reason))}_\n\n")

    if thin:
        lines.append("### Where this analysis is thin\n\n")
        lines.append("| Analyzer | Parsed | Produced a finding |\n|---|---:|---:|\n")
        for item in thin:
            lines.append(
                f"| `{item['analyzer']}` | {int(item['analyzed_files']):,} files | "
                f"{float(item['yield_ratio']):.0%} |\n"
            )
        lines.append(
            "\nThese analyzers read every eligible file but had little to say about "
            "them. Sections resting on them are correspondingly thin, and that is a "
            "limit of this tool rather than a statement about the code.\n\n"
        )

    return lines


def _claim_table(claims: tuple[RenderedClaim, ...]) -> Iterable[str]:
    yield "| Claim | Status | Confidence | Evidence |\n|---|---|---:|---|\n"
    for claim in claims:
        if claim.citations:
            # Contradicting receipts are what a reader most needs to see, so they
            # survive truncation ahead of the supporting majority.
            ordered = sorted(claim.citations, key=lambda item: item.relationship != "contradicts")
            shown = ordered[:MAX_RENDERED_CITATIONS]
            receipts = "<br/>".join(
                f"`{_escape(item.location)}`"
                + (f" `{item.file_sha256[:8]}`" if item.file_sha256 else "")
                + ("" if item.relationship == "supports" else " _(contradicts)_")
                for item in shown
            )
            remaining = len(ordered) - len(shown)
            if remaining:
                receipts += f"<br/>_+{remaining:,} further receipt(s); see the JSON projection._"
        else:
            receipts = "_none recorded_"
        yield (
            f"| {_escape(claim.claim)} | `{claim.status}` | {claim.confidence:.2f} | {receipts} |\n"
        )


def render_spec_markdown(document: SpecDocument) -> str:
    lines: list[str] = []
    section_titles = {item.section_id: f"§{item.number} {item.title}" for item in document.sections}
    # Excerpts are read from the analyzed tree, so the root is resolved once and
    # every citation path is checked to stay inside it.
    root = Path(document.root).expanduser().resolve()
    corpus_files = sum(int(item.get("analyzed_files", 0) or 0) for item in document.coverage)

    lines.append(f"# {document.profile_title}\n\n")
    lines.append(f"- Snapshot: `{document.snapshot_id}`\n")
    lines.append(f"- Repository: `{document.root}`\n")
    lines.append(f"- Profile: `{document.profile_id}`\n")
    lines.append(f"- Generated: {document.generated_at}\n")
    lines.append(
        f"- Claims in ledger: {document.total_claims:,} "
        f"({document.cited_claims:,} rendered with receipts)\n"
    )
    lines.append(f"- Stale claims against this snapshot: {document.stale_claim_count:,}\n\n")
    if document.profile_lineage:
        lines.append(f"_Outline lineage: {document.profile_lineage}_\n\n")

    lines.extend(_executive_summary(document))

    verdict_counts: dict[str, int] = {}
    for section in document.sections:
        verdict_counts[section.verdict] = verdict_counts.get(section.verdict, 0) + 1
    lines.append("## Determination summary\n\n| Verdict | Sections |\n|---|---:|\n")
    # Every verdict the profile defines, so the rows always sum to the section
    # count. Listing four of them dropped `not_applicable` sections from the
    # table entirely, which only stayed invisible while no run produced one.
    for verdict in VERDICTS:
        if verdict in verdict_counts:
            lines.append(f"| {verdict} | {verdict_counts[verdict]:,} |\n")
    lines.append("\n")

    lines.append("## Contents\n\n")
    for section in document.sections:
        indent = "  " * section.depth
        lines.append(f"{indent}- §{section.number} {section.title} — `{section.verdict}`\n")
    lines.append("\n")

    for section in document.sections:
        heading = "#" * min(6, section.depth + 2)
        lines.append(f"{heading} {section.number} {section.title}\n\n")
        if section.concern:
            lines.append(f"_Concern: {section.concern}_\n\n")

        total = sum(item.match_count for item in section.probe_results)
        # An absence is only as strong as the corpus it was measured against.
        # "The glob matched zero" invites the question zero out of what, and
        # the answer is already known here.
        sentence = _VERDICT_SENTENCE[section.verdict].format(
            total=total,
            findings=len(section.findings),
            corpus=corpus_files,
            claims=document.total_claims,
            snapshot=document.snapshot_id,
            threshold=section.degenerate_threshold,
            requires=", ".join(
                section_titles.get(item, item) for item in section.unmet_requirements
            ),
        )
        lines.append(f"{sentence}\n\n")

        matched_candidates = [item for item in section.candidate_results if item.match_count]
        if matched_candidates:
            described = "; ".join(
                f"{_escape(item.name)} ({item.match_count:,})" for item in matched_candidates
            )
            lines.append(
                f"_Adjacent to this concern and present: {described}. Those records "
                "exist and none of them satisfies the concern above, so the absence "
                "is a reading of what is here rather than only a query that missed._\n\n"
            )
        elif section.verdict == "absent":
            # Drawn from the concern's own probes as well as its adjacent ones.
            # Wiring this to candidates alone left 27 of 33 absent sections
            # naming 546 artifacts in a query column and none of them in a
            # sentence: §8.7 already asked after `structlog`, `loguru`,
            # `winston` and `pino`, and said so only as
            # `dependency_name: structlog, loguru, ...`.
            named = _absent_artifacts((*section.probe_results, *section.candidate_results))
            if named:
                lines.append(f"_{named}_\n\n")

        if section.framing:
            lines.append(f"{section.framing}\n\n")

        if section.probe_results:
            lines.append("**Determination and evidence**\n\n")
            lines.append("| Concern probe | Query executed | Matches |\n|---|---|---:|\n")
            for probe in section.probe_results:
                lines.append(
                    f"| {_escape(probe.name)} | `{_escape(probe.query)}` | "
                    f"{probe.match_count:,} |\n"
                )
            lines.append("\n")
            examples = [probe for probe in section.probe_results if probe.matches]
            if examples:
                legible = [item for item in examples if item.kind in LEGIBLE_PROBE_KINDS]
                opaque = [item for item in examples if item.kind not in LEGIBLE_PROBE_KINDS]
                if legible:
                    lines.append("Matched records:\n\n")
                    for probe in legible:
                        shown = ", ".join(f"`{_escape(item)}`" for item in probe.matches)
                        suffix = " …" if probe.match_count > len(probe.matches) else ""
                        lines.append(f"- {_escape(probe.name)}: {shown}{suffix}\n")
                    lines.append("\n")
                if opaque:
                    named = ", ".join(
                        f"{_escape(item.name)} ({item.match_count:,})" for item in opaque
                    )
                    lines.append(
                        f"_{named} matched by identifier. Those identifiers resolve in "
                        "`spec.json`; the claims they name are listed below._\n\n"
                    )

        if section.section_id == "surface.dossiers":
            lines.extend(render_dossiers(document.dossiers))

        for panel in section.panels:
            # A panel with nothing in it repeats a verdict the section already
            # gave. In an `absent` section that is nine words to say what the
            # determination said; in an `applicable` one it is a real finding,
            # because the concern is present and this facet of it is not.
            if not panel.rows and section.verdict in {"absent", "not_applicable", "structural"}:
                continue
            lines.append(f"**{panel.title}**\n\n")
            if not panel.rows:
                lines.append("_No entries._\n\n")
            else:
                lines.append("| " + " | ".join(panel.columns) + " |\n")
                lines.append(
                    "|"
                    + "|".join("---:" if item == "right" else "---" for item in panel.alignments)
                    + "|\n"
                )
                for row in panel.rows[:MAX_PANEL_ROWS]:
                    lines.append("| " + " | ".join(_escape(cell) for cell in row) + " |\n")
                withheld = len(panel.rows) - MAX_PANEL_ROWS
                if withheld > 0:
                    lines.append(
                        f"\n_{withheld:,} further row(s) are carried in `spec.json` and "
                        "`spec.index.json`, which scale with the repository where this "
                        "document scales with what is interesting in it._\n"
                    )
                lines.append("\n")
            if panel.note:
                lines.append(f"_{panel.note}_\n\n")

        if section.findings:
            lines.append("**Findings**\n\n")
            lines.extend(_claim_table(section.findings))
            if section.omitted_findings:
                lines.append(
                    f"\n_{section.omitted_findings:,} further claim(s) match this "
                    "section's selector and are available in the JSON projection "
                    "and the claim ledger._\n"
                )
            excerpts = _excerpt_block(root, section.findings)
            if excerpts:
                lines.append("\n**Source for the above**\n")
                lines.extend(excerpts)
            lines.append("\n")

        if section.verdict == "absent" and section.constraints:
            lines.append("**Observed constraints relevant to adopting this concern**\n\n")
            lines.extend(_claim_table(section.constraints))
            lines.append("\n")

        for diagram in section.diagrams:
            if diagram.mermaid is None:
                lines.append(f"_Diagram `{diagram.name}` omitted: {diagram.omitted_reason}_\n\n")
                continue
            lines.append(f"**{diagram.title}**\n\n")
            lines.append(f"```mermaid\n{diagram.mermaid}\n```\n\n")
            note = (
                f"_{diagram.node_count:,} nodes, {diagram.edge_count:,} edges"
                + (", truncated for readability" if diagram.truncated else "")
                + "._\n\n"
            )
            lines.append(note)

        if section.examined_files:
            # The reverse of a citation: not "where did this sentence come
            # from" but "which files did you read to write this section",
            # which is the question asked when deciding whether a section can
            # be trusted or has to be checked.
            top_files = section.examined_files[:MAX_EXAMINED_FILES]
            listed = ", ".join(f"`{path}` ({count})" for path, count in top_files)
            remaining = len(section.examined_files) - len(top_files)
            if remaining > 0:
                listed += f", and {remaining:,} more"
            lines.append(
                f"**Files examined** ({len(section.examined_files):,}, receipt count in "
                f"parentheses): {listed}.\n\n"
            )

        if section.cross_references:
            references = ", ".join(
                section_titles.get(item, item) for item in section.cross_references
            )
            lines.append(f"_Related: {references}._\n\n")

    lines.append("## Analyzer coverage\n\n")
    if document.coverage:
        lines.append(
            "| Analyzer | Language | Eligible | Analyzed | Coverage | Yield |\n"
            "|---|---|---:|---:|---:|---:|\n"
        )
        for item in document.coverage:
            eligible = int(item["eligible_files"])
            analyzed = int(item["analyzed_files"])
            # With nothing eligible, neither ratio means anything; a printed
            # percentage would invent precision the run does not have.
            coverage = f"{float(item['coverage_ratio']):.1%}" if eligible else "n/a"
            claim_yield = f"{float(item.get('yield_ratio', 0.0)):.1%}" if analyzed else "n/a"
            lines.append(
                f"| `{item['analyzer']}` | {item['language']} | "
                f"{eligible:,} | {analyzed:,} | {coverage} | {claim_yield} |\n"
            )
        lines.append(
            "\n_Coverage is the share of eligible files that parsed. Yield is the "
            "share of parsed files that produced at least one claim. A section may "
            "report full coverage and still rest on a thin analysis; the yield "
            "column is what distinguishes the two._\n"
        )
    else:
        lines.append("No analyzer coverage was recorded for this snapshot.\n")

    lines.append("\n## Interpretation boundary\n\n")
    lines.append(
        "Every determination in this document is a projection of the evidence ledger "
        "for one snapshot. A `present` verdict means declared probes matched counted "
        "records; an `absent` verdict means the listed queries returned nothing, which "
        "is evidence of absence only within the analyzed snapshot and the scope of "
        "those queries. Static evidence does not prove runtime behavior, and no "
        "section was written by a language model.\n"
    )
    return "".join(lines)


def render_spec_json(document: SpecDocument) -> str:
    return json.dumps(document.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_spec_index_json(document: SpecDocument) -> str:
    """The symbol inventory and name concordance, as their own document."""

    return json.dumps(document.index_to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
