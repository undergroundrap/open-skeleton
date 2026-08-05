# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Per-capability dossiers: everything known about one capability, in one place.

A ledger scatters facts by category. A reader onboarding to a feature does not
want the storage claims in one section and the state claims in another; they
want one page about the thing they are about to change.

This assembles that page from records already established elsewhere. It adds no
fact. Its value is entirely in the grouping — which is the same value the long
narrative sections of a conventional specification provide, without generating
prose to carry it.

The result is also what an agent needs: a bounded, self-contained brief about
one capability, rather than a whole document to search.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from open_skeleton.spec.capabilities import Capability
from open_skeleton.spec.consequences import Consequence

MAX_DOSSIER_CLAIMS = 8
MAX_DOSSIER_ITEMS = 6


@dataclass(frozen=True, slots=True)
class Dossier:
    """One capability, with everything the ledger relates to it."""

    capability_id: str
    label: str
    kind: str
    surface: tuple[str, ...]
    implementing_files: tuple[str, ...]
    findings: tuple[tuple[str, str, str], ...]
    touches_state: tuple[str, ...]
    verified_by: tuple[str, ...]
    consequences: tuple[str, ...]
    receipt_count: int

    @property
    def verification(self) -> str:
        return "exercised" if self.verified_by else "no-verifying-reference"

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "label": self.label,
            "kind": self.kind,
            "surface": list(self.surface),
            "implementing_files": list(self.implementing_files),
            "findings": [
                {"claim": claim, "status": status, "location": location}
                for claim, status, location in self.findings
            ],
            "touches_state": list(self.touches_state),
            "verified_by": list(self.verified_by),
            "consequences": list(self.consequences),
            "receipt_count": self.receipt_count,
            "verification": self.verification,
        }


# Categories describing state a capability's files own, rather than what they do.
_STATE_CATEGORIES = frozenset(
    {"process_local_state", "storage", "storage_schema", "storage_serialization"}
)


def build_dossiers(
    capabilities: tuple[Capability, ...],
    claims: tuple[dict[str, Any], ...],
    evidence_by_id: dict[str, dict[str, Any]],
    consequences: tuple[Consequence, ...] = (),
    *,
    limit: int = 20,
) -> tuple[Dossier, ...]:
    """Group ledger records under the capability whose files they touch."""

    claims_by_id = {str(item["claim_id"]): item for item in claims}

    def location(claim: dict[str, Any]) -> str:
        for evidence_id in claim.get("supporting_evidence", ()):
            record = evidence_by_id.get(evidence_id)
            if record is None or str(record["path"]) in {".", ""}:
                continue
            line = record["start_line"]
            return f"{record['path']}:{line}" if line else str(record["path"])
        return "—"

    def paths_of(claim: dict[str, Any]) -> set[str]:
        return {
            str(evidence_by_id[evidence_id]["path"])
            for evidence_id in claim.get("supporting_evidence", ())
            if evidence_id in evidence_by_id
        }

    consequence_by_claim: dict[str, list[str]] = {}
    for item in consequences:
        for claim_id in item.claim_ids:
            consequence_by_claim.setdefault(claim_id, []).append(item.statement)

    dossiers: list[Dossier] = []
    for capability in capabilities[:limit]:
        owned = set(capability.paths)
        if not owned:
            continue

        related = [
            claim
            for claim in claims
            if paths_of(claim) & owned and str(claim["claim_id"]) not in set(capability.claim_ids)
        ]
        state = [item for item in related if str(item["category"]) in _STATE_CATEGORIES]
        other = [item for item in related if str(item["category"]) not in _STATE_CATEGORIES]

        implied: list[str] = []
        for claim_id in (*capability.claim_ids, *(str(item["claim_id"]) for item in related)):
            for statement in consequence_by_claim.get(claim_id, []):
                if statement not in implied:
                    implied.append(statement)

        surface = capability.routes or capability.symbols
        dossiers.append(
            Dossier(
                capability_id=capability.capability_id,
                label=capability.label,
                kind=capability.kind,
                surface=tuple(surface[:MAX_DOSSIER_ITEMS]),
                implementing_files=tuple(capability.paths[:MAX_DOSSIER_ITEMS]),
                findings=tuple(
                    (str(item["claim"]), str(item["status"]), location(item))
                    for item in other[:MAX_DOSSIER_CLAIMS]
                ),
                touches_state=tuple(str(item["claim"]) for item in state[:MAX_DOSSIER_ITEMS]),
                verified_by=tuple(capability.exercised_by[:MAX_DOSSIER_ITEMS]),
                consequences=tuple(implied[:MAX_DOSSIER_ITEMS]),
                receipt_count=len(capability.evidence_ids)
                + sum(
                    len(claims_by_id.get(str(item["claim_id"]), {}).get("supporting_evidence", ()))
                    for item in related
                ),
            )
        )
    return tuple(dossiers)


def render_dossiers(dossiers: tuple[Dossier, ...]) -> list[str]:
    """Render each dossier as a compact briefing block."""

    if not dossiers:
        empty = (
            "_No capability carries an implementing file, so there is nothing to "
            "assemble a dossier from._\n\n"
        )
        return [empty]

    lines: list[str] = []
    for item in dossiers:
        lines.append(f"#### {item.capability_id} — `{item.label}`\n\n")
        lines.append(f"_{item.kind}, {item.receipt_count:,} receipts, {item.verification}._\n\n")
        if item.surface:
            entries = "".join(f"- `{value}`\n" for value in item.surface)
            lines.append(f"**Surface**\n\n{entries}\n")
        if item.implementing_files:
            entries = ", ".join(f"`{value}`" for value in item.implementing_files)
            lines.append(f"**Implemented in** {entries}\n\n")
        if item.touches_state:
            entries = "".join(f"- {value}\n" for value in item.touches_state)
            lines.append(f"**State it touches**\n\n{entries}\n")
        if item.findings:
            lines.append("**Findings in these files**\n\n")
            lines.append("| Finding | Status | Evidence |\n|---|---|---|\n")
            for claim, status, where in item.findings:
                escaped = claim.replace("|", "\\|")
                lines.append(f"| {escaped} | `{status}` | `{where}` |\n")
            lines.append("\n")
        if item.consequences:
            entries = "".join(f"- {value}\n" for value in item.consequences)
            lines.append(f"**What that implies**\n\n{entries}\n")
        if item.verified_by:
            entries = ", ".join(f"`{value}`" for value in item.verified_by)
            lines.append(f"**Exercised by** {entries}\n\n")
        else:
            lines.append(
                "**Exercised by** no test-role file or operator harness reaches this "
                "capability's symbols.\n\n"
            )
    return lines
