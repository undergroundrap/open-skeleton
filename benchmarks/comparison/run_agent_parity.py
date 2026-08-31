# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Plan or run blinded Claude/Codex proposals over a full parity corpus.

Plan creation is the default and never contacts a model. `--execute` is an
explicit paid-provider action. Each provider receives the same frozen baseline
block batch and complete candidate corpus, but never sees the other provider's
answer. Results remain proposals: deterministic reconciliation identifies
agreement and disputes, while denominator-changing and unjudgeable outcomes
always require human acceptance before a parity gate can pass.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

from run_comparison import (
    DEFAULT_BASELINE_INVENTORY,
    _load_baseline_record,
    _sha256,
    _verify_repository,
)
from run_parity_inventory import SCHEMA as CORPUS_SCHEMA
from run_parity_inventory import TOOL_ROOT, _require_private_output

from open_skeleton.providers import (
    REASONING_REVIEW_BATCH_CONTRACT,
    ClaudeCliProvider,
    CodexCliProvider,
    ProviderAdapter,
    ProviderRequest,
    _request_hash,
)

PLAN_SCHEMA = "open-skeleton.parity-review-plan.v1"
PROPOSALS_SCHEMA = "open-skeleton.parity-review-proposals.v1"
EXPECTED_REVIEWERS = ("codex", "claude")
REVIEW_RUBRIC = """\
Classify every supplied baseline block before comparing it.

Materiality:
- material: a repository, requirement, constraint, flow, risk, decision, fact, or conclusion.
- nonmaterial: navigation, generic connective prose, or boilerplate with no technical assertion.
- duplicate: repeats a conclusion already present in another supplied baseline block.
- code: source/example syntax whose material meaning belongs to surrounding prose.
- presentation_only: headings, separators, or layout with no independent assertion.
- unjudgeable: the block cannot be classified from the supplied artifacts.

For a material block, compare every semantic atom: subject, predicate, polarity,
modality, scope, preconditions, causal relationship, and consequence. Use:
- equivalent when every atom is preserved;
- candidate_superset when every atom is preserved and the candidate adds supported detail;
- partial when at least one atom is preserved and another is absent or weakened;
- missing only after searching the complete supplied candidate corpus;
- contradictory when the candidate makes an incompatible assertion;
- baseline_incorrect only when supplied repository evidence directly refutes it;
- unjudgeable when static evidence cannot decide.

Nonmaterial, duplicate, code, and presentation_only blocks use not_applicable.
Every relation is a proposal. Cite the baseline block evidence receipt. Equivalent,
candidate_superset, partial, and contradictory proposals must cite the candidate
unit IDs that support the comparison. Never infer missing from lexical retrieval:
the complete candidate corpus is supplied. A compound block is equivalent only
when all of its material atoms are equivalent or supersets.
"""


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def _load_corpus(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Parity corpus must be a JSON object")
    if document.get("schema") != CORPUS_SCHEMA:
        raise ValueError("Unsupported parity corpus schema")
    if document.get("sensitivity") != "private-source-derived":
        raise ValueError("Parity corpus must retain its private-source-derived label")
    if document.get("scope") != "full-document":
        raise ValueError("Parity review requires a full-document corpus")
    for side in ("baseline", "candidate"):
        accounting = document.get("accounting", {}).get(side, {})
        if accounting.get("nonblank_lines") != accounting.get(
            "accounted_nonblank_lines"
        ) or accounting.get("unaccounted_lines"):
            raise ValueError(f"Parity corpus has incomplete {side} line accounting")
        blocks = document.get(f"{side}_blocks")
        if not isinstance(blocks, list) or not blocks:
            raise ValueError(f"Parity corpus has no {side} blocks")
        ids = [item.get("id") for item in blocks if isinstance(item, dict)]
        if (
            len(ids) != len(blocks)
            or len(ids) != len(set(ids))
            or not all(isinstance(item, str) and item for item in ids)
        ):
            raise ValueError(f"Parity corpus has malformed or duplicate {side} block IDs")
        artifact = document.get(side, {})
        artifact_path = Path(str(artifact.get("path", "")))
        if not artifact_path.is_file():
            raise ValueError(f"Parity corpus {side} artifact is missing")
        if artifact_path.stat().st_size != artifact.get("bytes") or _sha256(
            artifact_path
        ) != artifact.get("sha256"):
            raise ValueError(f"Parity corpus {side} artifact changed after inventory")
    context = document.get("context")
    if context is not None:
        if not isinstance(context, dict):
            raise ValueError("Parity corpus context receipt is malformed")
        context_path = Path(str(context.get("path", "")))
        if not context_path.is_file():
            raise ValueError("Parity corpus context artifact is missing")
        if context_path.stat().st_size != context.get("bytes") or _sha256(
            context_path
        ) != context.get("sha256"):
            raise ValueError("Parity corpus context artifact changed after inventory")
    return cast(dict[str, Any], document)


def _verify_frozen_repository(corpus: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Recheck the clean fixture immediately before planning or execution."""

    repository = corpus.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("Parity corpus has no repository receipt")
    raw_path = repository.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("Parity corpus repository receipt has no path")
    path = Path(raw_path).resolve(strict=True)
    observed = _verify_repository(path, record)
    if observed.get("commit") != repository.get("commit"):
        raise ValueError("Repository changed after parity corpus inventory")
    return observed


def _build_plan(
    corpus: dict[str, Any],
    *,
    corpus_sha256: str,
    batch_size: int,
    reviewers: tuple[str, ...] = EXPECTED_REVIEWERS,
    models: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    if not 1 <= batch_size <= 100:
        raise ValueError("Parity review batch size must be between 1 and 100")
    if not reviewers or any(item not in EXPECTED_REVIEWERS for item in reviewers):
        raise ValueError("Parity reviewers must be codex and/or claude")
    if len(reviewers) != len(set(reviewers)):
        raise ValueError("Parity reviewers cannot be duplicated")
    selected_models = {reviewer: (models or {}).get(reviewer) for reviewer in reviewers}
    unit_ids = [str(item["id"]) for item in corpus["baseline_blocks"]]
    batches = []
    for offset in range(0, len(unit_ids), batch_size):
        selected = unit_ids[offset : offset + batch_size]
        batches.append(
            {
                "id": f"batch-{offset // batch_size + 1:04d}",
                "unit_ids": selected,
                "unit_count": len(selected),
            }
        )
    body = {
        "schema": PLAN_SCHEMA,
        "sensitivity": "private-source-derived",
        "scope": "full-document",
        "contacts_model": False,
        "corpus_sha256": corpus_sha256,
        "baseline_id": corpus["baseline"]["id"],
        "repository_commit": corpus["repository"]["commit"],
        "candidate_sha256": corpus["candidate"]["sha256"],
        "rubric_sha256": hashlib.sha256(REVIEW_RUBRIC.encode()).hexdigest(),
        "reviewers": list(reviewers),
        "reviewer_models": selected_models,
        "batch_size": batch_size,
        "batch_count": len(batches),
        "review_request_count": len(batches) * len(reviewers),
        "batches": batches,
    }
    return {**body, "plan_sha256": _canonical_hash(body)}


def _request_for_batch(
    corpus: dict[str, Any],
    batch: dict[str, Any],
    *,
    model: str | None,
    timeout_seconds: int,
) -> ProviderRequest:
    by_id = {str(item["id"]): item for item in corpus["baseline_blocks"]}
    review_units = [by_id[str(item)] for item in batch["unit_ids"]]
    evidence = [
        {
            "evidence_id": f"artifact:{item['id']}",
            "artifact": "registered-baseline",
            "path": corpus["baseline"]["path"],
            "start_line": item["start_line"],
            "end_line": item["end_line"],
            "text_sha256": item["text_sha256"],
            "relationship": "contains",
        }
        for item in review_units
    ]
    context = {
        "protocol": {
            "blinded": True,
            "peer_output_visible": False,
            "generation_session_reused": False,
            "candidate_corpus_complete": True,
            "review_output_is_proposal": True,
        },
        "baseline": {
            "id": corpus["baseline"]["id"],
            "sha256": corpus["baseline"]["sha256"],
        },
        "candidate": {"sha256": corpus["candidate"]["sha256"]},
        "repository": corpus["repository"],
        "review_units": review_units,
        "candidate_units": corpus["candidate_blocks"],
        "evidence": evidence,
    }
    return ProviderRequest(
        task=REVIEW_RUBRIC,
        snapshot_id=str(corpus["repository"]["commit"]),
        context_pack=context,
        output_contract=REASONING_REVIEW_BATCH_CONTRACT,
        model=model,
        timeout_seconds=timeout_seconds,
    )


def _adapter(name: str) -> ProviderAdapter:
    if name == "codex":
        return CodexCliProvider()
    if name == "claude":
        return ClaudeCliProvider()
    raise ValueError(f"Unsupported parity reviewer: {name}")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _load_resumable_result(path: Path, expected_hash: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("request_sha256") != expected_hash:
        return None
    return cast(dict[str, Any], document)


def _run_one(
    *,
    corpus: dict[str, Any],
    batch: dict[str, Any],
    reviewer: str,
    model: str | None,
    timeout_seconds: int,
    results_dir: Path,
    resume: bool,
) -> dict[str, Any]:
    request = _request_for_batch(corpus, batch, model=model, timeout_seconds=timeout_seconds)
    expected_hash = _request_hash(request)
    path = results_dir / f"{batch['id']}-{reviewer}.json"
    if resume and (existing := _load_resumable_result(path, expected_hash)) is not None:
        return existing
    result = _adapter(reviewer).generate(request, workspace=results_dir / "provider-workspace")
    artifact = {
        "schema": "open-skeleton.parity-review-provider-result.v1",
        "sensitivity": "private-source-derived",
        "batch_id": batch["id"],
        "reviewer": reviewer,
        "model": model,
        **result.to_dict(),
    }
    _atomic_json(path, artifact)
    return artifact


def _reconcile(
    corpus: dict[str, Any],
    plan: dict[str, Any],
    provider_results: list[dict[str, Any]],
) -> dict[str, Any]:
    reviews: dict[tuple[str, str], dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    reviewers = tuple(str(item) for item in plan["reviewers"])
    expected: dict[tuple[str, str], set[str]] = {
        (str(batch["id"]), reviewer): {str(item) for item in batch["unit_ids"]}
        for batch in plan["batches"]
        for reviewer in reviewers
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for result in provider_results:
        key = (str(result.get("batch_id")), str(result.get("reviewer")))
        grouped.setdefault(key, []).append(result)

    for key in sorted(set(grouped).difference(expected)):
        errors.append(
            {
                "batch_id": key[0],
                "reviewer": key[1],
                "error": "Unexpected provider result was excluded from reconciliation",
            }
        )
    for key, unit_ids in expected.items():
        matches = grouped.get(key, [])
        if len(matches) != 1:
            errors.append(
                {
                    "batch_id": key[0],
                    "reviewer": key[1],
                    "error": (
                        "Missing provider result"
                        if not matches
                        else "Duplicate provider results were excluded from reconciliation"
                    ),
                }
            )
            continue
        result = matches[0]
        reviewer = key[1]
        if result.get("status") != "complete" or not isinstance(result.get("output"), dict):
            errors.append(
                {
                    "batch_id": result.get("batch_id"),
                    "reviewer": reviewer,
                    "error": result.get("error"),
                }
            )
            continue
        output_reviews = result["output"].get("reviews")
        if (
            not isinstance(output_reviews, list)
            or {str(item.get("id")) for item in output_reviews if isinstance(item, dict)}
            != unit_ids
        ):
            errors.append(
                {
                    "batch_id": key[0],
                    "reviewer": reviewer,
                    "error": "Provider result unit coverage does not match the frozen batch",
                }
            )
            continue
        for review in output_reviews:
            reviews[(str(review["id"]), reviewer)] = review

    decisions: list[dict[str, Any]] = []
    for unit in corpus["baseline_blocks"]:
        unit_id = str(unit["id"])
        proposals = {name: reviews.get((unit_id, name)) for name in reviewers}
        if any(value is None for value in proposals.values()):
            state = "incomplete"
        else:
            signatures = {
                (
                    str(value["materiality"]),
                    str(value["baseline_validity"]),
                    str(value["status"]),
                    tuple(sorted(str(item) for item in value["candidate_unit_ids"])),
                )
                for value in proposals.values()
                if value is not None
            }
            if len(signatures) != 1:
                state = "disputed"
            else:
                only = next(value for value in proposals.values() if value is not None)
                if (
                    only["status"] in {"baseline_incorrect", "unjudgeable"}
                    or only["materiality"] == "unjudgeable"
                ):
                    state = "human_required"
                else:
                    state = "agent_consensus_proposal"
        decisions.append(
            {
                "id": unit_id,
                "state": state,
                "proposals": proposals,
            }
        )

    counts: dict[str, int] = {}
    for item in decisions:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return {
        "schema": PROPOSALS_SCHEMA,
        "sensitivity": "private-source-derived",
        "scope": "full-document",
        "corpus_sha256": plan["corpus_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "summary": {
            "baseline_blocks": len(corpus["baseline_blocks"]),
            "reviewers": list(reviewers),
            "states": dict(sorted(counts.items())),
            "provider_errors": len(errors),
            "parity_proven": False,
            "parity_blocker": (
                "Provider outputs are immutable proposals. Compound conclusions still require "
                "atom-level acceptance, every dispute and denominator-changing proposal requires "
                "human resolution, and a preregistered sample of agreements requires human audit."
            ),
        },
        "provider_errors": errors,
        "decisions": decisions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument(
        "--provider",
        action="append",
        choices=EXPECTED_REVIEWERS,
        help="Repeat for independent reviewers. Defaults to codex and claude.",
    )
    parser.add_argument("--codex-model")
    parser.add_argument("--claude-model")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Explicitly invoke selected providers. Without this flag only the plan is written.",
    )
    parser.add_argument("--baseline-inventory", type=Path, default=DEFAULT_BASELINE_INVENTORY)
    args = parser.parse_args()

    corpus = _load_corpus(args.corpus)
    record = _load_baseline_record(args.baseline_inventory, str(corpus["baseline"]["id"]))
    _verify_frozen_repository(corpus, record)
    reviewers = tuple(args.provider or EXPECTED_REVIEWERS)
    model_by_reviewer = {"codex": args.codex_model, "claude": args.claude_model}
    if args.execute:
        missing_models = [reviewer for reviewer in reviewers if not model_by_reviewer[reviewer]]
        if missing_models:
            raise ValueError(
                "Executed parity review requires an explicit model for every provider: "
                + ", ".join(missing_models)
            )
    output_dir = _require_private_output(args.output_dir, TOOL_ROOT)
    corpus_sha256 = _sha256(args.corpus)
    plan = _build_plan(
        corpus,
        corpus_sha256=corpus_sha256,
        batch_size=args.batch_size,
        reviewers=reviewers,
        models=model_by_reviewer,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(output_dir / "parity-review-plan.json", plan)

    if not args.execute:
        print(
            json.dumps(
                {
                    "contacts_model": False,
                    "baseline_blocks": len(corpus["baseline_blocks"]),
                    "candidate_blocks": len(corpus["candidate_blocks"]),
                    "batch_count": plan["batch_count"],
                    "review_request_count": plan["review_request_count"],
                    "plan_sha256": plan["plan_sha256"],
                    "artifact": str(output_dir / "parity-review-plan.json"),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if not 1 <= args.concurrency <= 16:
        raise ValueError("Parity review concurrency must be between 1 and 16")
    if not 1 <= args.timeout_seconds <= 3_600:
        raise ValueError("Parity review timeout must be between 1 and 3600 seconds")
    batches = plan["batches"]
    if args.max_batches is not None:
        if args.max_batches < 1:
            raise ValueError("--max-batches must be positive")
        batches = batches[: args.max_batches]
    jobs = [(batch, reviewer) for batch in batches for reviewer in reviewers]
    results_dir = output_dir / "provider-results"
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                _run_one,
                corpus=corpus,
                batch=batch,
                reviewer=reviewer,
                model=model_by_reviewer[reviewer],
                timeout_seconds=args.timeout_seconds,
                results_dir=results_dir,
                resume=args.resume,
            )
            for batch, reviewer in jobs
        ]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    proposals = _reconcile(corpus, plan, results)
    _atomic_json(output_dir / "parity-review-proposals.json", proposals)
    print(json.dumps(proposals["summary"], indent=2, sort_keys=True))
    return 0 if not proposals["provider_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
