# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Create human adjudication work or evaluate fixture-scoped semantic parity.

The two model reviewers prefill proposals, but this gate accepts no model output
as proof by itself. A named human must verify every baseline block and attest
that every semantic atom was checked. Any baseline-invalid decision requires a
second named human plus repository evidence so the denominator cannot be gamed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from run_agent_parity import (
    PROPOSALS_SCHEMA,
    _atomic_json,
    _load_corpus,
    _verify_frozen_repository,
)
from run_comparison import DEFAULT_BASELINE_INVENTORY, _load_baseline_record, _sha256
from run_parity_inventory import TOOL_ROOT, _require_private_output

ADJUDICATION_SCHEMA = "open-skeleton.parity-adjudication.v1"
PROOF_SCHEMA = "open-skeleton.parity-proof.v1"
PASSING_RELATIONS = frozenset({"equivalent", "candidate_superset"})
NONMATERIAL = frozenset({"nonmaterial", "duplicate", "code", "presentation_only"})


def _load_proposals(path: Path, corpus_sha256: str) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Parity proposals must be a JSON object")
    if document.get("schema") != PROPOSALS_SCHEMA:
        raise ValueError("Unsupported parity proposals schema")
    if document.get("sensitivity") != "private-source-derived":
        raise ValueError("Parity proposals must retain their sensitivity label")
    if document.get("scope") != "full-document":
        raise ValueError("Parity proof requires full-document proposals")
    if document.get("corpus_sha256") != corpus_sha256:
        raise ValueError("Parity proposals do not match the frozen corpus")
    decisions = document.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("Parity proposals have no decisions")
    ids = [item.get("id") for item in decisions if isinstance(item, dict)]
    if len(ids) != len(decisions) or len(ids) != len(set(ids)):
        raise ValueError("Parity proposals contain malformed or duplicate decision IDs")
    return cast(dict[str, Any], document)


def _proposal_signature(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "materiality": proposal["materiality"],
        "baseline_validity": proposal["baseline_validity"],
        "status": proposal["status"],
        "candidate_unit_ids": sorted(str(item) for item in proposal["candidate_unit_ids"]),
    }


def _consensus(decision: dict[str, Any]) -> dict[str, Any] | None:
    if decision.get("state") != "agent_consensus_proposal":
        return None
    proposals = decision.get("proposals")
    if not isinstance(proposals, dict) or not proposals:
        return None
    values = [item for item in proposals.values() if isinstance(item, dict)]
    if len(values) != len(proposals):
        return None
    signatures = {_canonical_signature(_proposal_signature(item)) for item in values}
    return values[0] if len(signatures) == 1 else None


def _canonical_signature(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _adjudication_template(
    corpus: dict[str, Any],
    proposals: dict[str, Any],
    *,
    corpus_sha256: str,
    proposals_sha256: str,
) -> dict[str, Any]:
    decisions = {str(item["id"]): item for item in proposals["decisions"]}
    baseline_ids = [str(item["id"]) for item in corpus["baseline_blocks"]]
    if set(decisions) != set(baseline_ids):
        raise ValueError("Parity proposals do not cover the frozen baseline exactly")
    adjudications = []
    for unit_id in baseline_ids:
        proposal = _consensus(decisions[unit_id])
        signature = _proposal_signature(proposal) if proposal else {}
        adjudications.append(
            {
                "id": unit_id,
                "proposal_disposition": "accept_consensus" if proposal else "override",
                "materiality": signature.get("materiality"),
                "baseline_validity": signature.get("baseline_validity"),
                "status": signature.get("status"),
                "candidate_unit_ids": signature.get("candidate_unit_ids", []),
                "all_atoms_verified": False,
                "rationale": "",
                "reviewer": "",
                "second_reviewer": "",
                "repository_evidence_ids": [],
            }
        )
    return {
        "schema": ADJUDICATION_SCHEMA,
        "sensitivity": "private-source-derived",
        "scope": "full-document",
        "corpus_sha256": corpus_sha256,
        "proposals_sha256": proposals_sha256,
        "instructions": (
            "A named human must inspect every baseline block and all of its semantic atoms. "
            "Set all_atoms_verified true and record a rationale. Use accept_consensus only "
            "after checking the prefilled relation; otherwise use override. Baseline-invalid "
            "decisions require a distinct second reviewer and repository evidence IDs."
        ),
        "adjudications": adjudications,
    }


def _evaluate_one(
    item: dict[str, Any],
    decision: dict[str, Any],
    candidate_ids: set[str],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    disposition = item.get("proposal_disposition")
    if disposition not in {"accept_consensus", "override"}:
        reasons.append("proposal_disposition must be accept_consensus or override")
    if item.get("all_atoms_verified") is not True:
        reasons.append("all semantic atoms were not human-verified")
    reviewer = item.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        reasons.append("a named human reviewer is required")
    if not isinstance(item.get("rationale"), str) or not item["rationale"].strip():
        reasons.append("a human rationale is required")

    materiality = item.get("materiality")
    validity = item.get("baseline_validity")
    status = item.get("status")
    raw_candidate_ids = item.get("candidate_unit_ids")
    if not isinstance(raw_candidate_ids, list) or not all(
        isinstance(value, str) for value in raw_candidate_ids
    ):
        reasons.append("candidate_unit_ids must be a string list")
        selected_ids: set[str] = set()
    else:
        selected_ids = set(raw_candidate_ids)
        if len(selected_ids) != len(raw_candidate_ids):
            reasons.append("candidate_unit_ids cannot contain duplicates")
        if not selected_ids.issubset(candidate_ids):
            reasons.append("candidate_unit_ids contain unknown candidate blocks")

    if disposition == "accept_consensus":
        proposal = _consensus(decision)
        if proposal is None:
            reasons.append("there is no exact dual-agent consensus to accept")
        elif _proposal_signature(proposal) != {
            "materiality": materiality,
            "baseline_validity": validity,
            "status": status,
            "candidate_unit_ids": sorted(selected_ids),
        }:
            reasons.append("accepted fields differ from the frozen consensus proposal")

    if materiality == "material":
        if validity == "supported":
            if status not in PASSING_RELATIONS:
                reasons.append(
                    "a supported material block is not equivalent or a candidate superset"
                )
            if not selected_ids:
                reasons.append("a passing material relation requires candidate block evidence")
        elif validity == "incorrect":
            if status != "baseline_incorrect":
                reasons.append("an incorrect baseline must use baseline_incorrect")
            second = item.get("second_reviewer")
            if (
                not isinstance(second, str)
                or not second.strip()
                or second.strip() == str(reviewer).strip()
            ):
                reasons.append("baseline-invalid removal requires a distinct second reviewer")
            evidence_ids = item.get("repository_evidence_ids")
            if (
                not isinstance(evidence_ids, list)
                or not evidence_ids
                or not all(isinstance(value, str) and value for value in evidence_ids)
            ):
                reasons.append("baseline-invalid removal requires repository evidence IDs")
        else:
            reasons.append("material baseline validity is unresolved")
    elif materiality in NONMATERIAL:
        if status != "not_applicable":
            reasons.append("nonmaterial blocks must use not_applicable")
        if selected_ids:
            reasons.append("nonmaterial blocks cannot cite candidate relation blocks")
    else:
        reasons.append("materiality is unresolved")
    return not reasons, reasons


def _evaluate(
    corpus: dict[str, Any],
    proposals: dict[str, Any],
    adjudications: dict[str, Any],
    *,
    corpus_sha256: str,
    proposals_sha256: str,
    repository_receipt: dict[str, Any],
) -> dict[str, Any]:
    if adjudications.get("schema") != ADJUDICATION_SCHEMA:
        raise ValueError("Unsupported parity adjudication schema")
    if adjudications.get("sensitivity") != "private-source-derived":
        raise ValueError("Parity adjudications must retain their sensitivity label")
    if adjudications.get("scope") != "full-document":
        raise ValueError("Parity adjudication must cover the full document")
    if adjudications.get("corpus_sha256") != corpus_sha256:
        raise ValueError("Parity adjudication does not match the frozen corpus")
    if adjudications.get("proposals_sha256") != proposals_sha256:
        raise ValueError("Parity adjudication does not match the frozen proposals")

    decisions = {str(item["id"]): item for item in proposals["decisions"]}
    items = adjudications.get("adjudications")
    if not isinstance(items, list):
        raise ValueError("Parity adjudication has no adjudications list")
    by_id = {str(item.get("id")): item for item in items if isinstance(item, dict)}
    baseline_ids = {str(item["id"]) for item in corpus["baseline_blocks"]}
    if len(by_id) != len(items) or set(by_id) != baseline_ids or set(decisions) != baseline_ids:
        raise ValueError("Parity adjudication must cover every baseline block exactly once")

    candidate_ids = {str(item["id"]) for item in corpus["candidate_blocks"]}
    results = []
    for unit in corpus["baseline_blocks"]:
        unit_id = str(unit["id"])
        passed, reasons = _evaluate_one(by_id[unit_id], decisions[unit_id], candidate_ids)
        results.append({"id": unit_id, "passed": passed, "reasons": reasons})
    passed_count = sum(1 for item in results if item["passed"])
    total = len(results)
    return {
        "schema": PROOF_SCHEMA,
        "sensitivity": "private-source-derived",
        "scope": "registered-fixture-full-document-semantic-coverage",
        "corpus_sha256": corpus_sha256,
        "proposals_sha256": proposals_sha256,
        "repository": repository_receipt,
        "summary": {
            "baseline_blocks": total,
            "human_verified_blocks": passed_count,
            "failed_blocks": total - passed_count,
            "parity_proven": total > 0 and passed_count == total,
            "claim_boundary": (
                "The result applies only to this hash-pinned artifact and repository fixture; "
                "it is not a universal product-quality or repository-class claim."
            ),
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--proposals", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--adjudications", type=Path)
    parser.add_argument("--write-template", action="store_true")
    parser.add_argument("--baseline-inventory", type=Path, default=DEFAULT_BASELINE_INVENTORY)
    args = parser.parse_args()
    if args.write_template == (args.adjudications is not None):
        raise ValueError("Choose exactly one of --write-template or --adjudications")

    corpus_sha256 = _sha256(args.corpus)
    corpus = _load_corpus(args.corpus)
    record = _load_baseline_record(args.baseline_inventory, str(corpus["baseline"]["id"]))
    repository_receipt = _verify_frozen_repository(corpus, record)
    proposals_sha256 = _sha256(args.proposals)
    proposals = _load_proposals(args.proposals, corpus_sha256)
    output_dir = _require_private_output(args.output_dir, TOOL_ROOT)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.write_template:
        template = _adjudication_template(
            corpus,
            proposals,
            corpus_sha256=corpus_sha256,
            proposals_sha256=proposals_sha256,
        )
        artifact = output_dir / "parity-adjudication.json"
        _atomic_json(artifact, template)
        print(json.dumps({"parity_proven": False, "artifact": str(artifact)}, indent=2))
        return 0

    adjudications = json.loads(args.adjudications.read_text(encoding="utf-8"))
    proof = _evaluate(
        corpus,
        proposals,
        adjudications,
        corpus_sha256=corpus_sha256,
        proposals_sha256=proposals_sha256,
        repository_receipt=repository_receipt,
    )
    artifact = output_dir / "parity-proof.json"
    _atomic_json(artifact, proof)
    print(json.dumps(proof["summary"], indent=2, sort_keys=True))
    return 0 if proof["summary"]["parity_proven"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
