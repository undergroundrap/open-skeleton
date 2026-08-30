# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Deterministic, source-grounded work packets for narrative agents.

The local analyzers are faster and more exhaustive at census work than a
language model reading files one at a time. A model is better at explaining a
bounded collection of already-established facts. This module makes that split
explicit: one job per non-structural outline obligation, with exact claim IDs,
two-sided evidence, absence probes, and a fixed task contract.

Building a plan never contacts a model. Jobs are independent and therefore can
be dispatched in parallel by an external orchestrator, while the specification
and its coherence checks remain the authoritative deterministic projection.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from open_skeleton.ledger import EvidenceLedger
from open_skeleton.models import utc_now
from open_skeleton.spec.render import RenderedSection, SpecDocument

SYNTHESIS_PLAN_SCHEMA = "open-skeleton.synthesis_plan.v1"

_TASK = (
    "Explain this specification obligation using only the supplied context pack. "
    "Describe the current implementation, important flows or boundaries, and failure "
    "or maintenance consequences that follow from the cited claims. Preserve conflict "
    "and unknown states. Do not propose unrequested functionality. Every factual finding "
    "must cite claim_ids from the pack. When the verdict is absence-based, explain the "
    "probe scope and do not turn absence into a claim of compliance."
)


def _priority(section: RenderedSection) -> str:
    claims = (*section.findings, *section.constraints)
    if any(item.status == "conflict" for item in claims):
        return "critical"
    if section.unmet_requirements or any(item.status == "unknown" for item in claims):
        return "high"
    if any(item.importance in {"critical", "high"} for item in claims):
        return "high"
    if section.verdict in {"absent", "not_applicable"}:
        return "low"
    return "normal"


def _claim_ids(section: RenderedSection) -> tuple[str, ...]:
    routed = (
        *(item.claim_id for item in section.findings),
        *(item.claim_id for item in section.constraints),
        *section.omitted_claim_ids,
    )
    return tuple(dict.fromkeys(routed))


def build_synthesis_plan(
    document: SpecDocument,
    ledger: EvidenceLedger,
    *,
    max_chars: int = 20_000,
    max_claims: int = 100,
) -> dict[str, Any]:
    """Build one bounded job for every decidable outline obligation."""

    jobs: list[dict[str, Any]] = []
    for section in document.sections:
        if section.verdict == "structural":
            continue
        claim_ids = _claim_ids(section)
        context = ledger.context_pack_for_claims(
            document.snapshot_id,
            claim_ids,
            query=f"{section.section_id}: {section.concern}",
            max_chars=max_chars,
            max_claims=max_claims,
        )
        context["obligation"] = {
            "section_id": section.section_id,
            "number": section.number,
            "title": section.title,
            "concern": section.concern,
            "framing": section.framing,
            "verdict": section.verdict,
            "probes": [item.to_dict() for item in section.probe_results],
            "candidate_probes": [item.to_dict() for item in section.candidate_results],
            "unmet_requirements": list(section.unmet_requirements),
        }
        jobs.append(
            {
                "job_id": section.section_id,
                "priority": _priority(section),
                "task": _TASK,
                "parallel_safe": True,
                "requested_claim_count": len(claim_ids),
                "included_claim_count": len(context["claims"]),
                "context_pack": context,
            }
        )

    priority_counts = Counter(str(item["priority"]) for item in jobs)
    verdict_counts = Counter(str(item["context_pack"]["obligation"]["verdict"]) for item in jobs)
    return {
        "schema": SYNTHESIS_PLAN_SCHEMA,
        "snapshot_id": document.snapshot_id,
        "profile_id": document.profile_id,
        "generated_at": utc_now(),
        "contacts_model": False,
        "job_count": len(jobs),
        "priority_counts": dict(sorted(priority_counts.items())),
        "verdict_counts": dict(sorted(verdict_counts.items())),
        "jobs": jobs,
    }
