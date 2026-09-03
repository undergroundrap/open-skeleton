# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    """Return a stable, timezone-explicit timestamp for persisted events."""

    return datetime.now(UTC).isoformat(timespec="milliseconds")


def writable_text(value: str) -> str:
    """The same text, in a form a UTF-8 document can actually hold.

    `pygments/unistring.py` names the Unicode categories, and the category of
    surrogates is spelled with surrogates: `Cs = '\\ud800-\\udbff...'`. Those
    code points cannot be encoded as UTF-8 by definition. Read out of the
    source and carried into a claim, one of them ended the run at the last
    step -- `spec.json` failed to write and the entire specification for a
    339-file repository was lost, after every file had been read correctly.

    A repository is not wrong to contain them. Unicode tables, text
    processors and fuzzing corpora all do, so a specification generator that
    dies on them is not general. The escape spelling is also the truer
    rendering: the source writes `\\ud800`, and printing the code point was
    already a transformation this engine chose to make.

    The check is a fast path -- almost every string passes on the first try
    -- so this costs an encode on text that was about to be encoded anyway.
    """

    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "".join(
            character if _encodable(character) else f"\\u{ord(character):04x}"
            for character in value
        )
    return value


def _encodable(character: str) -> bool:
    try:
        character.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def writable_structure(value: Any) -> Any:
    """`writable_text` through the nested metadata a symbol carries.

    The first attempt at this fixed claim text alone and the run still died
    at the same character. The constant was recorded twice -- once in prose a
    reader sees and once in `SymbolRecord.metadata`, which is what the
    constants panel is actually built from. Fixing the visible half and
    declaring victory is how the same defect gets found twice.
    """

    if isinstance(value, str):
        return writable_text(value)
    if isinstance(value, dict):
        return {writable_text(str(key)): writable_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [writable_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(writable_structure(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    sha256: str
    size_bytes: int
    line_count: int
    language: str
    role: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExclusionRecord:
    path: str
    reason: str
    # How many files an excluded *directory* took with it. One row saying
    # `gitignored:[Ll]ibrary/` stood for 2,449 files in a real Unity project
    # while the census reported "16 excluded entries", which understated the
    # drop by two orders of magnitude in a document whose stated principle is
    # that a census which silently drops files overstates its own coverage.
    #
    # Zero for a single excluded file, where the row already is the count.
    contained_files: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScanEvent:
    stage: str
    message: str
    created_at: str = field(default_factory=utc_now)
    path: str | None = None
    processed_files: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Snapshot:
    snapshot_id: str
    root: Path
    policy_version: str
    created_at: str
    duration_ms: int
    files: tuple[FileRecord, ...]
    exclusions: tuple[ExclusionRecord, ...]
    events: tuple[ScanEvent, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)

    @property
    def total_lines(self) -> int:
        return sum(item.line_count for item in self.files)

    def summary(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "root": str(self.root),
            "policy_version": self.policy_version,
            "created_at": self.created_at,
            "duration_ms": self.duration_ms,
            "file_count": len(self.files),
            "excluded_count": len(self.exclusions),
            "total_bytes": self.total_bytes,
            "total_lines": self.total_lines,
        }


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    snapshot_id: str
    path: str
    start_line: int | None
    end_line: int | None
    symbol: str | None
    evidence_kind: str
    excerpt_sha256: str | None
    analyzer: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    symbol_id: str
    snapshot_id: str
    path: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    language: str
    analyzer: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "qualified_name", writable_text(self.qualified_name))
        object.__setattr__(self, "metadata", writable_structure(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    edge_id: str
    snapshot_id: str
    source_symbol_id: str | None
    source_path: str
    relationship: str
    target_ref: str
    target_symbol_id: str | None
    evidence_id: str | None
    analyzer: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    claim_id: str
    snapshot_id: str
    claim: str
    category: str
    status: str
    confidence: float
    importance: str
    produced_by: str
    created_at: str
    supporting_evidence: tuple[str, ...] = ()
    contradicting_evidence: tuple[str, ...] = ()
    invalidation_keys: tuple[str, ...] = ()
    alternative_hypotheses: tuple[str, ...] = ()
    verified_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim", writable_text(self.claim))
        if self.status not in {"verified", "inferred", "conflict", "unknown", "stale"}:
            raise ValueError(f"Unsupported claim status: {self.status}")
        if self.importance not in {"critical", "high", "medium", "low"}:
            raise ValueError(f"Unsupported claim importance: {self.importance}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Claim confidence must be between 0 and 1")
        if self.status == "verified" and not self.supporting_evidence:
            raise ValueError("Verified claims require supporting evidence")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CoverageRecord:
    analyzer: str
    language: str
    eligible_files: int
    analyzed_files: int
    failed_files: int
    unsupported_files: int
    failures: tuple[str, ...] = ()
    claimed_files: int = 0

    @property
    def coverage_ratio(self) -> float:
        """Share of eligible files the analyzer successfully parsed.

        This measures reach, not understanding. A file that parses cleanly and
        produces nothing still counts here — see :attr:`yield_ratio`.
        """

        return self.analyzed_files / self.eligible_files if self.eligible_files else 1.0

    @property
    def yield_ratio(self) -> float:
        """Share of parsed files that produced at least one claim.

        Reported alongside coverage because the two diverge in exactly the case
        a reader most needs to know about: an analyzer whose grammar handles a
        language but whose claim vocabulary has nothing to say about this kind
        of program. High coverage with low yield means "we read it all and
        found little", which is a different statement from "we analyzed it".
        """

        return self.claimed_files / self.analyzed_files if self.analyzed_files else 0.0

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["coverage_ratio"] = self.coverage_ratio
        result["yield_ratio"] = self.yield_ratio
        return result


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    snapshot_id: str
    analyzer_version: str
    created_at: str
    duration_ms: int
    symbols: tuple[SymbolRecord, ...]
    edges: tuple[EdgeRecord, ...]
    evidence: tuple[EvidenceRecord, ...]
    claims: tuple[ClaimRecord, ...]
    coverage: tuple[CoverageRecord, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "analyzer_version": self.analyzer_version,
            "created_at": self.created_at,
            "duration_ms": self.duration_ms,
            "symbol_count": len(self.symbols),
            "edge_count": len(self.edges),
            "evidence_count": len(self.evidence),
            "claim_count": len(self.claims),
            "coverage": [item.to_dict() for item in self.coverage],
        }
