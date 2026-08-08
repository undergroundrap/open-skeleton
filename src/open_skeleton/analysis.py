# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from open_skeleton.analyzers.base import Analyzer
from open_skeleton.analyzers.hum_semantic_index import HumSemanticIndexAnalyzer
from open_skeleton.analyzers.project_metadata import ProjectMetadataAnalyzer
from open_skeleton.analyzers.python_ast import PythonAstAnalyzer
from open_skeleton.analyzers.rust_lexical import RustLexicalAnalyzer
from open_skeleton.analyzers.typescript_lexical import TypeScriptLexicalAnalyzer
from open_skeleton.ids import stable_id
from open_skeleton.models import (
    AnalysisResult,
    ClaimRecord,
    CoverageRecord,
    EdgeRecord,
    EvidenceRecord,
    Snapshot,
    SymbolRecord,
    utc_now,
)

PIPELINE_VERSION = "deterministic-pipeline/v1"
EXPONENTIAL_BASE_PATTERN = re.compile(r"exponentiates base ([0-9]+(?:\.[0-9]+)?)")


def _append_orphan_candidates(
    snapshot: Snapshot,
    *,
    created_at: str,
    symbols: tuple[SymbolRecord, ...],
    edges: tuple[EdgeRecord, ...],
    evidence: tuple[EvidenceRecord, ...],
    claims: list[ClaimRecord],
) -> tuple[EvidenceRecord, ...]:
    """Add conservative static orphan candidates using the resolved import census."""

    python_modules = [
        item
        for item in symbols
        if item.kind == "module" and item.analyzer.startswith("python-ast/")
    ]
    imports = [item for item in edges if item.relationship == "imports"]
    evidence_by_symbol = {
        item.symbol: item
        for item in evidence
        if item.evidence_kind == "module" and item.symbol is not None
    }
    file_roles = {item.path: item.role for item in snapshot.files}
    entrypoint_paths = {
        invalidation.removeprefix("file:")
        for claim in claims
        if claim.category == "application_entry"
        for invalidation in claim.invalidation_keys
        if invalidation.startswith("file:")
    }

    def module_is_imported(module: str) -> bool:
        candidates = {module}
        parts = module.split(".")
        if len(parts) > 1:
            candidates.add(".".join(parts[1:]))
        return any(
            target == candidate or target.startswith(f"{candidate}.")
            for candidate in candidates
            for edge in imports
            for target in (edge.target_ref.lstrip("."),)
        )

    imported_modules = {
        module.qualified_name
        for module in python_modules
        if module_is_imported(module.qualified_name)
    }
    census = EvidenceRecord(
        evidence_id=stable_id(
            "evidence",
            (snapshot.snapshot_id, ".", "static_import_census", PIPELINE_VERSION),
        ),
        snapshot_id=snapshot.snapshot_id,
        path=".",
        start_line=None,
        end_line=None,
        symbol=None,
        evidence_kind="static_import_census",
        excerpt_sha256=snapshot.snapshot_id,
        analyzer=PIPELINE_VERSION,
        created_at=created_at,
    )
    added = False
    for module in python_modules:
        if (
            module.path.endswith("/__init__.py")
            or module.path == "__init__.py"
            or file_roles.get(module.path) != "source"
            or module.path in entrypoint_paths
            or module.qualified_name in imported_modules
        ):
            continue
        parent = module.qualified_name.rsplit(".", 1)[0] if "." in module.qualified_name else ""
        imported_siblings = [
            sibling
            for sibling in python_modules
            if sibling.qualified_name.rsplit(".", 1)[0] == parent
            and sibling.qualified_name in imported_modules
        ]
        if not imported_siblings:
            continue
        module_evidence = evidence_by_symbol.get(module.qualified_name)
        if module_evidence is None:
            continue
        text = (
            f"{module.path} has no resolved inbound static Python import while "
            f"{len(imported_siblings)} sibling modules do; treat it as an orphan candidate, "
            "not a deletion instruction."
        )
        claims.append(
            ClaimRecord(
                claim_id=stable_id(
                    "claim",
                    (snapshot.snapshot_id, "orphan_candidate", text, PIPELINE_VERSION),
                ),
                snapshot_id=snapshot.snapshot_id,
                claim=text,
                category="orphan_candidate",
                status="inferred",
                confidence=0.95,
                importance="medium",
                produced_by=PIPELINE_VERSION,
                created_at=created_at,
                supporting_evidence=(module_evidence.evidence_id, census.evidence_id),
                invalidation_keys=(
                    f"file:{module.path}",
                    f"module:{module.qualified_name}",
                    "python:import-graph",
                ),
                alternative_hypotheses=(
                    (
                        "The module may be loaded dynamically, invoked directly, imported by an "
                        "external consumer, or retained intentionally as a reference."
                    ),
                ),
            )
        )
        added = True
    return evidence + ((census,) if added else ())


def _append_mathematical_conflicts(
    snapshot: Snapshot,
    *,
    created_at: str,
    evidence: tuple[EvidenceRecord, ...],
    claims: list[ClaimRecord],
) -> tuple[EvidenceRecord, ...]:
    scaling_claims: list[tuple[float, ClaimRecord]] = []
    for claim in claims:
        if claim.category != "exponential_scaling":
            continue
        match = EXPONENTIAL_BASE_PATTERN.search(claim.claim)
        if match:
            scaling_claims.append((float(match.group(1)), claim))
    distinct_bases = sorted({base for base, _ in scaling_claims})
    if len(distinct_bases) < 2:
        return evidence

    file_records = {item.path: item for item in snapshot.files}
    comment_receipts: list[EvidenceRecord] = []
    for path, file_record in file_records.items():
        if file_record.language != "Python":
            continue
        source_path = snapshot.root / Path(path)
        payload = source_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != file_record.sha256:
            continue
        source = payload.decode("utf-8", errors="strict")
        for line_number, line in enumerate(source.splitlines(keepends=True), start=1):
            lowered = line.casefold()
            if "never trivial" not in lowered and "wall forever" not in lowered:
                continue
            comment_receipts.append(
                EvidenceRecord(
                    evidence_id=stable_id(
                        "evidence",
                        (
                            snapshot.snapshot_id,
                            path,
                            line_number,
                            "exponential_comment",
                            PIPELINE_VERSION,
                        ),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    path=path,
                    start_line=line_number,
                    end_line=line_number,
                    symbol=None,
                    evidence_kind="documentation_assertion",
                    excerpt_sha256=hashlib.sha256(line.encode("utf-8")).hexdigest(),
                    analyzer=PIPELINE_VERSION,
                    created_at=created_at,
                )
            )
    if not comment_receipts:
        return evidence

    lower, upper = distinct_bases[0], distinct_bases[-1]
    if lower <= 0 or upper <= lower:
        return evidence
    supporting = tuple(
        sorted(
            {
                evidence_id
                for base, claim in scaling_claims
                if base in {lower, upper}
                for evidence_id in claim.supporting_evidence
            }
        )
    )
    text = (
        f"Ascension-related exponential bases {upper:g} and {lower:g} imply a relative "
        f"factor of ({upper:g}/{lower:g})^N, which is unbounded as N grows; source comments "
        "claiming a fixed wall stays non-trivial forever conflict with those formulas."
    )
    claims.append(
        ClaimRecord(
            claim_id=stable_id(
                "claim", (snapshot.snapshot_id, "mathematical_conflict", text, PIPELINE_VERSION)
            ),
            snapshot_id=snapshot.snapshot_id,
            claim=text,
            category="mathematical_conflict",
            status="conflict",
            confidence=1.0,
            importance="high",
            produced_by=PIPELINE_VERSION,
            created_at=created_at,
            supporting_evidence=supporting,
            contradicting_evidence=tuple(item.evidence_id for item in comment_receipts),
            invalidation_keys=tuple(sorted({f"file:{item.path}" for item in comment_receipts})),
            alternative_hypotheses=(
                (
                    "A finite ascension cap or another faster-growing mechanic could bound the "
                    "advantage, but neither follows from the compared exponentials alone."
                ),
            ),
        )
    )
    return evidence + tuple(comment_receipts)


def _append_testing_census(
    snapshot: Snapshot,
    *,
    created_at: str,
    evidence: tuple[EvidenceRecord, ...],
    claims: list[ClaimRecord],
) -> tuple[EvidenceRecord, ...]:
    test_files = tuple(item for item in snapshot.files if item.role == "test")
    if test_files:
        return evidence
    census = EvidenceRecord(
        evidence_id=stable_id(
            "evidence", (snapshot.snapshot_id, ".", "test_role_census", PIPELINE_VERSION)
        ),
        snapshot_id=snapshot.snapshot_id,
        path=".",
        start_line=None,
        end_line=None,
        symbol=None,
        evidence_kind="snapshot_census",
        excerpt_sha256=snapshot.snapshot_id,
        analyzer=PIPELINE_VERSION,
        created_at=created_at,
    )
    text = (
        "The bounded snapshot contains no files classified as conventional test files; "
        "operator or validation scripts may still exist under non-test paths."
    )
    claims.append(
        ClaimRecord(
            claim_id=stable_id(
                "claim", (snapshot.snapshot_id, "testing_census", text, PIPELINE_VERSION)
            ),
            snapshot_id=snapshot.snapshot_id,
            claim=text,
            category="testing_census",
            status="verified",
            confidence=1.0,
            importance="medium",
            produced_by=PIPELINE_VERSION,
            created_at=created_at,
            verified_at=created_at,
            supporting_evidence=(census.evidence_id,),
            invalidation_keys=("snapshot:file-set",),
            alternative_hypotheses=(
                "Tests may use project-specific naming conventions not recognized by the scanner.",
            ),
        )
    )
    return (*evidence, census)


def _append_route_documentation_conflicts(
    snapshot: Snapshot,
    *,
    created_at: str,
    claims: list[ClaimRecord],
) -> None:
    source = next((item for item in claims if item.category == "http_route_inventory"), None)
    documented = next(
        (item for item in claims if item.category == "documented_http_route_inventory"),
        None,
    )
    if source is None or documented is None:
        return
    source_count = re.search(r"declares (\d+) HTTP route", source.claim)
    documented_count = re.search(r"document (\d+) distinct HTTP", documented.claim)
    if (
        not source_count
        or not documented_count
        or source_count.group(1) == documented_count.group(1)
    ):
        return
    source_value = int(source_count.group(1))
    documented_value = int(documented_count.group(1))
    text = (
        f"Markdown API tables document {documented_value} endpoints while Python source "
        f"declares {source_value} HTTP route handlers."
    )
    claims.append(
        ClaimRecord(
            claim_id=stable_id(
                "claim", (snapshot.snapshot_id, "api_documentation_drift", text, PIPELINE_VERSION)
            ),
            snapshot_id=snapshot.snapshot_id,
            claim=text,
            category="api_documentation_drift",
            status="conflict",
            confidence=1.0,
            importance="high",
            produced_by=PIPELINE_VERSION,
            created_at=created_at,
            supporting_evidence=documented.supporting_evidence,
            contradicting_evidence=source.supporting_evidence,
            invalidation_keys=tuple(
                sorted({*documented.invalidation_keys, *source.invalidation_keys})
            ),
        )
    )


def _append_dependency_conflicts(
    snapshot: Snapshot,
    *,
    created_at: str,
    edges: tuple[EdgeRecord, ...],
    evidence: tuple[EvidenceRecord, ...],
    claims: list[ClaimRecord],
) -> None:
    declared = {
        item.target_ref.casefold().replace("_", "-")
        for item in edges
        if item.relationship == "declares_dependency"
    }
    if not declared:
        return
    local_names = {
        part.casefold().replace("_", "-")
        for file_record in snapshot.files
        if file_record.language == "Python"
        for part in (*Path(file_record.path).parts[:-1], Path(file_record.path).stem)
    }
    standard = {item.casefold().replace("_", "-") for item in sys.stdlib_module_names}
    requirements_evidence = tuple(
        item.evidence_id for item in evidence if item.evidence_kind == "requirements_manifest"
    )
    imports: dict[str, set[str]] = {}
    for edge in edges:
        if edge.relationship != "imports" or not edge.source_path.startswith("scripts/"):
            continue
        top_level = edge.target_ref.lstrip(".").split(".", 1)[0]
        normalized = top_level.casefold().replace("_", "-")
        if (
            not normalized
            or normalized in standard
            or normalized in local_names
            or normalized in declared
            or edge.evidence_id is None
        ):
            continue
        imports.setdefault(normalized, set()).add(edge.evidence_id)
    for dependency, import_evidence in sorted(imports.items()):
        text = (
            f"Operator scripts import {dependency}, but the analyzed requirements manifests "
            f"do not declare {dependency}."
        )
        claims.append(
            ClaimRecord(
                claim_id=stable_id(
                    "claim", (snapshot.snapshot_id, "dependency_drift", text, PIPELINE_VERSION)
                ),
                snapshot_id=snapshot.snapshot_id,
                claim=text,
                category="dependency_drift",
                status="conflict",
                confidence=0.98,
                importance="high",
                produced_by=PIPELINE_VERSION,
                created_at=created_at,
                supporting_evidence=tuple(sorted(import_evidence)),
                contradicting_evidence=requirements_evidence,
                invalidation_keys=("snapshot:file-set", "python:import-graph"),
                alternative_hypotheses=(
                    (
                        "The dependency may be installed manually, transitively, or by an untracked "
                        "environment definition."
                    ),
                ),
            )
        )


def _attribute_claim_yield(
    results: Sequence[AnalysisResult],
) -> tuple[CoverageRecord, ...]:
    """Count, per analyzer, how many parsed files actually produced a claim.

    Coverage alone answers "did the file parse", which overstates how much an
    analyzer understood. A claim is attributed to every real file its supporting
    receipts point at; repository-wide census receipts (path ``.``) name no file
    and are excluded, so a single "no CI workflow exists" claim cannot make a
    whole language look productive.
    """

    attributed: list[CoverageRecord] = []
    for result in results:
        receipts = {item.evidence_id: item.path for item in result.evidence}
        claimed_paths = {
            path
            for claim in result.claims
            for evidence_id in (*claim.supporting_evidence, *claim.contradicting_evidence)
            for path in (receipts.get(evidence_id),)
            if path is not None and path not in {".", ""} and not path.startswith("@")
        }
        for record in result.coverage:
            attributed.append(
                replace(
                    record,
                    claimed_files=min(len(claimed_paths), record.analyzed_files),
                )
            )
    return tuple(attributed)


AnalysisEventCallback = Callable[[str, int, int], None]


def _merge_duplicate_claims(claims: list[ClaimRecord]) -> list[ClaimRecord]:
    """Fold claims sharing an identifier into one, keeping every receipt.

    Two sites can state the same fact. Rust's ``#[cfg]`` is the ordinary case:
    a trait is implemented once for Windows and once for everything else, both
    impls are real, and exactly one compiles per platform. The claim text is
    identical, so both records carry the same identifier.

    Storing them separately was impossible -- the ledger keys claims by that
    identifier -- and storing them in sequence deleted the first receipt while
    writing the second, so the run reported 635 claims and persisted 634 with
    one citation pointing at whichever impl happened to be written last.
    Merging the evidence keeps both lines, which is also the more useful
    reading: it shows a reader that the fact holds by two different routes.
    """

    merged: dict[str, ClaimRecord] = {}
    for claim in sorted(claims, key=lambda item: item.claim_id):
        existing = merged.get(claim.claim_id)
        if existing is None:
            merged[claim.claim_id] = claim
            continue
        merged[claim.claim_id] = replace(
            existing,
            supporting_evidence=_union(existing.supporting_evidence, claim.supporting_evidence),
            contradicting_evidence=_union(
                existing.contradicting_evidence, claim.contradicting_evidence
            ),
            invalidation_keys=_union(existing.invalidation_keys, claim.invalidation_keys),
            alternative_hypotheses=_union(
                existing.alternative_hypotheses, claim.alternative_hypotheses
            ),
        )
    return list(merged.values())


def _union(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    """Both sequences, order preserved, without repeats."""

    return tuple(dict.fromkeys((*first, *second)))


def build_analyzers(hum_index: Sequence[Path] | Path | None = None) -> tuple[Analyzer, ...]:
    """The analyzers a run consults, in the order their claims are merged.

    Declared at module scope and annotated with the protocol so conformance is
    checked rather than assumed. As a tuple built inside the run function the
    annotation had nowhere to attach, and `Analyzer` went unimported by
    anything -- a contract five classes were expected to satisfy and none was
    measured against.
    """

    return (
        PythonAstAnalyzer(),
        TypeScriptLexicalAnalyzer(),
        RustLexicalAnalyzer(),
        ProjectMetadataAnalyzer(),
        HumSemanticIndexAnalyzer(hum_index),
    )


def analyze_snapshot(
    snapshot: Snapshot,
    *,
    hum_index: Sequence[Path] | Path | None = None,
    on_event: AnalysisEventCallback | None = None,
) -> AnalysisResult:
    """Run deterministic semantic adapters and merge their immutable outputs."""

    started = time.perf_counter()
    created_at = utc_now()
    results = []
    for analyzer in build_analyzers(hum_index):
        result = analyzer.analyze(snapshot)
        results.append(result)
        if on_event is not None:
            on_event(
                result.analyzer_version,
                round((time.perf_counter() - started) * 1000),
                len(result.claims),
            )

    symbols = tuple(item for result in results for item in result.symbols)
    edges = tuple(item for result in results for item in result.edges)
    evidence = tuple(item for result in results for item in result.evidence)
    claims = [item for result in results for item in result.claims]
    coverage = _attribute_claim_yield(results)

    evidence = _append_orphan_candidates(
        snapshot,
        created_at=created_at,
        symbols=symbols,
        edges=edges,
        evidence=evidence,
        claims=claims,
    )
    _append_route_documentation_conflicts(
        snapshot,
        created_at=created_at,
        claims=claims,
    )
    _append_dependency_conflicts(
        snapshot,
        created_at=created_at,
        edges=edges,
        evidence=evidence,
        claims=claims,
    )
    evidence = _append_mathematical_conflicts(
        snapshot,
        created_at=created_at,
        evidence=evidence,
        claims=claims,
    )
    evidence = _append_testing_census(
        snapshot,
        created_at=created_at,
        evidence=evidence,
        claims=claims,
    )

    for file_record in snapshot.files:
        if file_record.role not in {"source", "test"} or file_record.line_count < 1_000:
            continue
        # A long test file concentrates the suite, not the architecture.
        # Both are worth knowing and they answer different questions, so the
        # claim says which one it is rather than presenting them as one list.
        in_test = str(file_record.role) == "test"
        text = (
            f"{file_record.path} concentrates {file_record.line_count} lines of test code."
            if in_test
            else f"{file_record.path} is an architectural concentration point at "
            f"{file_record.line_count} lines."
        )
        evidence_id = stable_id(
            "inventory-evidence",
            (snapshot.snapshot_id, file_record.path, file_record.sha256, "file_concentration"),
        )
        inventory_evidence = EvidenceRecord(
            evidence_id=evidence_id,
            snapshot_id=snapshot.snapshot_id,
            path=file_record.path,
            start_line=1,
            end_line=file_record.line_count,
            symbol=None,
            evidence_kind="file_inventory",
            excerpt_sha256=file_record.sha256,
            analyzer=PIPELINE_VERSION,
            created_at=created_at,
        )
        evidence += (inventory_evidence,)
        claims.append(
            ClaimRecord(
                claim_id=stable_id(
                    "claim",
                    (snapshot.snapshot_id, "concentration", text, PIPELINE_VERSION),
                ),
                snapshot_id=snapshot.snapshot_id,
                claim=text,
                category="test_concentration" if in_test else "concentration",
                status="verified",
                confidence=1.0,
                importance="medium" if in_test else "high",
                produced_by=PIPELINE_VERSION,
                created_at=created_at,
                verified_at=created_at,
                supporting_evidence=(evidence_id,),
                invalidation_keys=(f"file:{file_record.path}",),
            )
        )

    return AnalysisResult(
        snapshot_id=snapshot.snapshot_id,
        analyzer_version=PIPELINE_VERSION,
        created_at=created_at,
        duration_ms=round((time.perf_counter() - started) * 1000),
        symbols=tuple(
            sorted(symbols, key=lambda item: (item.path, item.start_line, item.symbol_id))
        ),
        edges=tuple(sorted(edges, key=lambda item: item.edge_id)),
        evidence=tuple(sorted(evidence, key=lambda item: item.evidence_id)),
        claims=tuple(_merge_duplicate_claims(claims)),
        coverage=coverage,
    )
