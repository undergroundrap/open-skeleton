# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Citation integrity for a rendered specification.

A generated document is only as trustworthy as its weakest citation. Every
receipt referenced by a spec is re-resolved here against the ledger and against
the current bytes on disk, so a stale or unresolvable citation is reported as a
number rather than discovered by a reader.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from open_skeleton.ledger import EvidenceLedger
from open_skeleton.spec.render import SpecDocument

CITATION_STATUSES = (
    "current",
    "source-changed",
    "file-missing",
    "unresolvable",
    "virtual",
)


@dataclass(frozen=True, slots=True)
class CitationCheck:
    evidence_id: str
    section_id: str
    claim_id: str
    location: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "section_id": self.section_id,
            "claim_id": self.claim_id,
            "location": self.location,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class CitationReport:
    snapshot_id: str
    total: int
    counts: dict[str, int]
    failures: tuple[CitationCheck, ...]

    @property
    def integrity(self) -> float:
        if self.total == 0:
            return 1.0
        resolvable = self.counts.get("current", 0) + self.counts.get("virtual", 0)
        return resolvable / self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "total_citations": self.total,
            "counts": dict(self.counts),
            "citation_integrity": self.integrity,
            "failures": [item.to_dict() for item in self.failures],
        }


def _file_digest(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def verify_spec(
    document: SpecDocument,
    ledger: EvidenceLedger,
    *,
    root: Path | None = None,
) -> CitationReport:
    """Re-resolve every citation in a spec against the ledger and current sources."""

    repository_root = (root or Path(document.root)).expanduser().resolve()
    digest_cache: dict[str, str | None] = {}
    checks: list[CitationCheck] = []

    for section in document.sections:
        for claim in (*section.findings, *section.constraints):
            for citation in claim.citations:
                record = ledger.get_evidence(citation.evidence_id)
                if record is None:
                    status = "unresolvable"
                elif citation.path == "." or citation.file_sha256 is None:
                    status = "virtual"
                else:
                    source = (repository_root / Path(citation.path)).resolve()
                    try:
                        source.relative_to(repository_root)
                    except ValueError:
                        status = "unresolvable"
                    else:
                        if citation.path not in digest_cache:
                            digest_cache[citation.path] = (
                                _file_digest(source) if source.is_file() else None
                            )
                        current = digest_cache[citation.path]
                        if current is None:
                            status = "file-missing"
                        elif current != citation.file_sha256:
                            status = "source-changed"
                        else:
                            status = "current"
                checks.append(
                    CitationCheck(
                        evidence_id=citation.evidence_id,
                        section_id=section.section_id,
                        claim_id=claim.claim_id,
                        location=citation.location,
                        status=status,
                    )
                )

    counts = dict.fromkeys(CITATION_STATUSES, 0)
    for check in checks:
        counts[check.status] += 1
    failures = tuple(check for check in checks if check.status not in {"current", "virtual"})
    return CitationReport(
        snapshot_id=document.snapshot_id,
        total=len(checks),
        counts=counts,
        failures=failures,
    )
