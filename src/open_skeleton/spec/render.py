# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from open_skeleton.ledger import EvidenceLedger
from open_skeleton.models import utc_now
from open_skeleton.spec.capabilities import Capability, build_capabilities
from open_skeleton.spec.diagrams import Diagram, build_diagram
from open_skeleton.spec.panels import Panel, PanelContext, build_panel
from open_skeleton.spec.probes import LedgerCorpus, ProbeResult, evaluate_section
from open_skeleton.spec.profile import SpecProfile, SpecSection, SpecSelector

SPEC_SCHEMA_VERSION = "open-skeleton.spec.v1"

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
        "zero matches against snapshot `{snapshot}`. The queries are listed below so "
        "the absence can be re-checked rather than trusted."
    ),
    "structural": (
        "This section organizes the subsections below and makes no presence claim "
        "of its own."
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
    degenerate_threshold: int = 0

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
            "depth": self.depth,
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "capabilities": [item.to_dict() for item in self.capabilities],
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


def _select(
    selector: SpecSelector | None,
    claims: Iterable[dict[str, Any]],
    used: set[str],
) -> tuple[list[dict[str, Any]], int]:
    if selector is None:
        return [], 0
    eligible = [
        claim
        for claim in claims
        if selector.accepts(claim) and claim["claim_id"] not in used
    ]
    return eligible[: selector.limit], max(0, len(eligible) - selector.limit)


def _citations(
    claim: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]
) -> tuple[Citation, ...]:
    citations: list[Citation] = []
    pairs = [
        (evidence_id, "supports")
        for evidence_id in claim.get("supporting_evidence", ())
    ] + [
        (evidence_id, "contradicts")
        for evidence_id in claim.get("contradicting_evidence", ())
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
    claims = tuple(ledger.list_claims(resolved_id, limit=5_000))
    symbols = tuple(ledger.list_symbols(resolved_id, limit=5_000))
    edges = tuple(ledger.list_edges(resolved_id))
    evidence_by_id = {
        str(item["evidence_id"]): item for item in ledger.list_evidence(resolved_id)
    }
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
    panel_context = PanelContext(
        files=files,
        exclusions=exclusions,
        snapshot=snapshot_row,
        capabilities=capabilities,
    )

    used: set[str] = set()
    rendered: list[RenderedSection] = []

    def render_node(section: SpecSection, depth: int) -> None:
        verdict, probe_results = evaluate_section(section, corpus)
        selected, omitted = _select(section.findings, claims, used)
        used.update(str(item["claim_id"]) for item in selected)
        constraint_claims, _ = _select(section.constraints, claims, set())

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
            build_diagram(name, files=files, claims=claims, symbols=symbols, edges=edges)
            for name in section.diagrams
        )
        panels = tuple(build_panel(name, panel_context) for name in section.panels)

        rendered.append(
            RenderedSection(
                section_id=section.section_id,
                number=section.number,
                title=section.title,
                concern=section.concern,
                framing=section.framing,
                verdict=verdict,
                probe_results=probe_results,
                findings=findings,
                constraints=constraints,
                diagrams=diagrams,
                panels=panels,
                cross_references=section.cross_references,
                omitted_findings=omitted,
                depth=depth,
                degenerate_threshold=section.degenerate_below,
            )
        )
        for child in section.children:
            render_node(child, depth + 1)

    for section in profile.sections:
        render_node(section, 0)

    cited = sum(
        1
        for item in rendered
        for claim in item.findings
        if claim.citations
    )
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
        capabilities=capabilities,
    )


def _escape(value: str) -> str:
    return value.replace("|", "\\|")


MAX_RENDERED_CITATIONS = 6


def _claim_table(claims: tuple[RenderedClaim, ...]) -> Iterable[str]:
    yield "| Claim | Status | Confidence | Evidence |\n|---|---|---:|---|\n"
    for claim in claims:
        if claim.citations:
            # Contradicting receipts are what a reader most needs to see, so they
            # survive truncation ahead of the supporting majority.
            ordered = sorted(
                claim.citations, key=lambda item: item.relationship != "contradicts"
            )
            shown = ordered[:MAX_RENDERED_CITATIONS]
            receipts = "<br/>".join(
                f"`{_escape(item.location)}`"
                + (f" `{item.file_sha256[:8]}`" if item.file_sha256 else "")
                + ("" if item.relationship == "supports" else " _(contradicts)_")
                for item in shown
            )
            remaining = len(ordered) - len(shown)
            if remaining:
                receipts += (
                    f"<br/>_+{remaining:,} further receipt(s); see the JSON projection._"
                )
        else:
            receipts = "_none recorded_"
        yield (
            f"| {_escape(claim.claim)} | `{claim.status}` | "
            f"{claim.confidence:.2f} | {receipts} |\n"
        )


def render_spec_markdown(document: SpecDocument) -> str:
    lines: list[str] = []
    section_titles = {
        item.section_id: f"§{item.number} {item.title}" for item in document.sections
    }

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

    verdict_counts: dict[str, int] = {}
    for section in document.sections:
        verdict_counts[section.verdict] = verdict_counts.get(section.verdict, 0) + 1
    lines.append("## Determination summary\n\n| Verdict | Sections |\n|---|---:|\n")
    for verdict in ("applicable", "degenerate", "absent", "structural"):
        if verdict in verdict_counts:
            lines.append(f"| {verdict} | {verdict_counts[verdict]:,} |\n")
    lines.append("\n")

    lines.append("## Contents\n\n")
    for section in document.sections:
        indent = "  " * section.depth
        lines.append(
            f"{indent}- §{section.number} {section.title} — `{section.verdict}`\n"
        )
    lines.append("\n")

    for section in document.sections:
        heading = "#" * min(6, section.depth + 2)
        lines.append(f"{heading} {section.number} {section.title}\n\n")
        if section.concern:
            lines.append(f"_Concern: {section.concern}_\n\n")

        total = sum(item.match_count for item in section.probe_results)
        sentence = _VERDICT_SENTENCE[section.verdict].format(
            total=total,
            snapshot=document.snapshot_id,
            threshold=section.degenerate_threshold,
        )
        lines.append(f"{sentence}\n\n")
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
                lines.append("Matched records:\n\n")
                for probe in examples:
                    shown = ", ".join(f"`{_escape(item)}`" for item in probe.matches)
                    suffix = " …" if probe.match_count > len(probe.matches) else ""
                    lines.append(f"- {_escape(probe.name)}: {shown}{suffix}\n")
                lines.append("\n")

        for panel in section.panels:
            lines.append(f"**{panel.title}**\n\n")
            if not panel.rows:
                lines.append("_No entries._\n\n")
            else:
                lines.append("| " + " | ".join(panel.columns) + " |\n")
                lines.append(
                    "|"
                    + "|".join(
                        "---:" if item == "right" else "---"
                        for item in panel.alignments
                    )
                    + "|\n"
                )
                for row in panel.rows:
                    lines.append(
                        "| " + " | ".join(_escape(cell) for cell in row) + " |\n"
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
            lines.append("\n")

        if section.verdict == "absent" and section.constraints:
            lines.append(
                "**Observed constraints relevant to adopting this concern**\n\n"
            )
            lines.extend(_claim_table(section.constraints))
            lines.append("\n")

        for diagram in section.diagrams:
            if diagram.mermaid is None:
                lines.append(
                    f"_Diagram `{diagram.name}` omitted: {diagram.omitted_reason}_\n\n"
                )
                continue
            lines.append(f"**{diagram.title}**\n\n")
            lines.append(f"```mermaid\n{diagram.mermaid}\n```\n\n")
            note = (
                f"_{diagram.node_count:,} nodes, {diagram.edge_count:,} edges"
                + (", truncated for readability" if diagram.truncated else "")
                + "._\n\n"
            )
            lines.append(note)

        if section.cross_references:
            references = ", ".join(
                section_titles.get(item, item) for item in section.cross_references
            )
            lines.append(f"_Related: {references}._\n\n")

    lines.append("## Analyzer coverage\n\n")
    if document.coverage:
        lines.append(
            "| Analyzer | Language | Eligible | Analyzed | Coverage |\n|---|---|---:|---:|---:|\n"
        )
        for item in document.coverage:
            lines.append(
                f"| `{item['analyzer']}` | {item['language']} | "
                f"{int(item['eligible_files']):,} | {int(item['analyzed_files']):,} | "
                f"{float(item['coverage_ratio']):.1%} |\n"
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
