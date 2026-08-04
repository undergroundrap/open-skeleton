# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import tracemalloc
from pathlib import Path
from typing import Any

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.exports import export_analysis_jsonl, export_analysis_markdown
from open_skeleton.models import AnalysisResult, ClaimRecord, EvidenceRecord, Snapshot, utc_now
from open_skeleton.scanner import scan_repository


BENCHMARK_SCHEMA = "open-skeleton.benchmark.v1"
OUTCOME_CREDIT = {"hit": 1.0, "partial": 0.5, "incorrect": 0.0, "miss": 0.0}


def _load_gold(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != BENCHMARK_SCHEMA:
        raise ValueError(f"Unsupported benchmark schema in {path}")
    if not isinstance(document.get("claims"), list) or not document["claims"]:
        raise ValueError("Benchmark gold must contain a non-empty claims list")
    identifiers = [item.get("id") for item in document["claims"] if isinstance(item, dict)]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Benchmark claim IDs must be unique")
    return document


def _git_commit(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _claim_matches(claim: ClaimRecord, specification: dict[str, Any]) -> bool:
    matcher = specification.get("match", {})
    if matcher.get("category") and claim.category != matcher["category"]:
        return False
    statuses = matcher.get("statuses")
    if statuses and claim.status not in statuses:
        return False
    return all(
        re.search(pattern, claim.claim, flags=re.IGNORECASE) is not None
        for pattern in matcher.get("all_patterns", [])
    )


def _receipt_is_current(
    receipt: EvidenceRecord,
    *,
    snapshot: Snapshot,
    files_by_path: dict[str, Any],
) -> bool:
    if receipt.path == ".":
        return receipt.excerpt_sha256 == snapshot.snapshot_id
    file_record = files_by_path.get(receipt.path)
    if file_record is None:
        return False
    try:
        payload = (snapshot.root / receipt.path).read_bytes()
        source = payload.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return False
    if hashlib.sha256(payload).hexdigest() != file_record.sha256:
        return False
    if receipt.start_line is None or receipt.end_line is None:
        expected = file_record.sha256
    else:
        lines = source.splitlines(keepends=True)
        excerpt = "".join(lines[receipt.start_line - 1 : receipt.end_line])
        expected = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    return receipt.excerpt_sha256 == expected


def _score_open_skeleton(
    gold: dict[str, Any],
    snapshot: Snapshot,
    analysis: AnalysisResult,
) -> dict[str, Any]:
    evidence_by_id = {item.evidence_id: item for item in analysis.evidence}
    files_by_path = {item.path: item for item in snapshot.files}
    rows: list[dict[str, Any]] = []
    matched_claim_ids: set[str] = set()
    total_weight = 0.0
    hit_weight = 0.0
    evidence_weight = 0.0

    for specification in gold["claims"]:
        weight = float(specification.get("weight", 1.0))
        total_weight += weight
        matches = [item for item in analysis.claims if _claim_matches(item, specification)]
        match = matches[0] if matches else None
        evidence_correct = False
        expected_paths = specification.get("evidence_paths_any", [])
        if match is not None:
            matched_claim_ids.add(match.claim_id)
            receipts = [
                evidence_by_id.get(item)
                for item in (*match.supporting_evidence, *match.contradicting_evidence)
            ]
            receipts = [item for item in receipts if item is not None]
            current = bool(receipts) and all(
                _receipt_is_current(item, snapshot=snapshot, files_by_path=files_by_path)
                for item in receipts
            )
            expected_path_found = not expected_paths or any(
                re.search(pattern, item.path, flags=re.IGNORECASE)
                for pattern in expected_paths
                for item in receipts
            )
            conflict_receipt = (
                match.status != "conflict" or bool(match.contradicting_evidence)
            )
            evidence_correct = current and expected_path_found and conflict_receipt
            hit_weight += weight
            if evidence_correct:
                evidence_weight += weight
        rows.append(
            {
                "id": specification["id"],
                "area": specification.get("area"),
                "expected": specification["statement"],
                "weight": weight,
                "outcome": "hit" if match is not None else "miss",
                "matched_claim": match.claim if match is not None else None,
                "matched_claim_id": match.claim_id if match is not None else None,
                "evidence_correct": evidence_correct,
            }
        )

    precision_categories = set(gold.get("precision_scope_categories", []))
    scoped_claims = [item for item in analysis.claims if item.category in precision_categories]
    correct_scoped = sum(item.claim_id in matched_claim_ids for item in scoped_claims)
    conflicts = [item for item in gold["claims"] if item.get("expected_status") == "conflict"]
    detected_conflicts = sum(
        row["outcome"] == "hit" and row["evidence_correct"]
        for row in rows
        if row["id"] in {item["id"] for item in conflicts}
    )
    return {
        "recall": hit_weight / total_weight if total_weight else None,
        "precision": correct_scoped / len(scoped_claims) if scoped_claims else None,
        "precision_scope_claims": len(scoped_claims),
        "precision_scope_correct": correct_scoped,
        "evidence_correctness": evidence_weight / hit_weight if hit_weight else None,
        "conflict_detection": detected_conflicts / len(conflicts) if conflicts else None,
        "matched_weight": hit_weight,
        "total_weight": total_weight,
        "claims": rows,
    }


def _score_baseline(gold: dict[str, Any]) -> dict[str, Any] | None:
    baseline = gold.get("baseline")
    if not isinstance(baseline, dict):
        return None
    total_weight = 0.0
    recall_credit = 0.0
    emitted_weight = 0.0
    precision_credit = 0.0
    evidence_denominator = 0.0
    evidence_credit = 0.0
    conflict_total = 0.0
    conflict_credit = 0.0
    rows = []
    for specification in gold["claims"]:
        weight = float(specification.get("weight", 1.0))
        adjudication = specification.get("baseline", {})
        outcome = adjudication.get("outcome", "miss")
        if outcome not in OUTCOME_CREDIT:
            raise ValueError(f"Unsupported baseline outcome for {specification['id']}: {outcome}")
        credit = OUTCOME_CREDIT[outcome]
        total_weight += weight
        recall_credit += weight * credit
        if outcome != "miss":
            emitted_weight += weight
            precision_credit += weight * credit
            evidence_denominator += weight
            evidence_label = adjudication.get("evidence", "none")
            evidence_credit += weight * {"correct": 1.0, "partial": 0.5, "none": 0.0}.get(
                evidence_label, 0.0
            )
        if specification.get("expected_status") == "conflict":
            conflict_total += weight
            conflict_credit += weight if outcome == "hit" else 0.0
        rows.append(
            {
                "id": specification["id"],
                "outcome": outcome,
                "evidence": adjudication.get("evidence", "none"),
                "notes": adjudication.get("notes"),
            }
        )
    return {
        "name": baseline.get("name", "baseline"),
        "recall": recall_credit / total_weight if total_weight else None,
        "precision": precision_credit / emitted_weight if emitted_weight else None,
        "evidence_correctness": (
            evidence_credit / evidence_denominator if evidence_denominator else None
        ),
        "conflict_detection": conflict_credit / conflict_total if conflict_total else None,
        "measurements": baseline.get("measurements", {}),
        "claims": rows,
        "scoring_note": (
            "Precision is limited to baseline statements mapped to this material-claim gold set; "
            "it is not a full-document precision estimate."
        ),
    }


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def run_benchmark(
    repository: Path,
    gold_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    root = repository.expanduser().resolve(strict=True)
    gold_file = gold_path.expanduser().resolve(strict=True)
    output = output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    gold = _load_gold(gold_file)
    expected_commit = gold.get("fixture", {}).get("commit")
    actual_commit = _git_commit(root)
    if expected_commit and actual_commit != expected_commit:
        raise ValueError(
            f"Fixture commit mismatch: expected {expected_commit}, observed {actual_commit or 'unavailable'}"
        )

    tracemalloc.start()
    started = time.perf_counter()
    snapshot = scan_repository(root)
    first_finding_ms: int | None = None

    def progress(_analyzer: str, elapsed_ms: int, claim_count: int) -> None:
        nonlocal first_finding_ms
        if first_finding_ms is None and claim_count:
            first_finding_ms = round((time.perf_counter() - started) * 1000)

    analysis = analyze_snapshot(snapshot, on_event=progress)
    total_ms = round((time.perf_counter() - started) * 1000)
    _, peak_allocated = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    jsonl_path = output / "analysis.jsonl"
    markdown_path = output / "analysis.md"
    export_analysis_jsonl(analysis, jsonl_path)
    export_analysis_markdown(analysis, markdown_path)
    markdown = markdown_path.read_text(encoding="utf-8")
    open_score = _score_open_skeleton(gold, snapshot, analysis)
    open_score["measurements"] = {
        "time_to_first_finding_ms": first_finding_ms,
        "total_time_ms": total_ms,
        "python_peak_allocated_bytes": peak_allocated,
        "included_files": len(snapshot.files),
        "included_lines": snapshot.total_lines,
        "output_characters": len(markdown),
        "output_words": _word_count(markdown),
        "output_lines": len(markdown.splitlines()),
        "claim_count": len(analysis.claims),
        "evidence_count": len(analysis.evidence),
    }
    result = {
        "schema_version": BENCHMARK_SCHEMA,
        "created_at": utc_now(),
        "fixture": {
            **gold.get("fixture", {}),
            "root": str(root),
            "actual_commit": actual_commit,
            "snapshot_id": snapshot.snapshot_id,
        },
        "gold_path": str(gold_file),
        "open_skeleton": open_score,
        "baseline": _score_baseline(gold),
        "limitations": [
            "One repository cannot establish broad product superiority.",
            "Precision is measured only over explicitly enumerated material categories.",
            "Peak allocation is Python tracemalloc data, not whole-process resident memory.",
            "Baseline outcomes are manual adjudications against a supplied static artifact.",
        ],
    }
    json_path = output / "benchmark.json"
    json_path.write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _write_markdown_report(result, output / "benchmark.md")
    return result


def _format_metric(value: float | None) -> str:
    return "not measured" if value is None else f"{value:.1%}"


def _write_markdown_report(result: dict[str, Any], path: Path) -> None:
    current = result["open_skeleton"]
    baseline = result.get("baseline")
    lines = [
        "# Comparative benchmark\n",
        f"- Fixture commit: `{result['fixture'].get('actual_commit')}`\n",
        f"- Snapshot: `{result['fixture']['snapshot_id']}`\n",
        "- Scope: independently enumerated material claims for this fixture\n\n",
        "## Scores\n\n",
        "| System | Recall | Scoped precision | Evidence correctness | Conflict detection |\n",
        "|---|---:|---:|---:|---:|\n",
        (
            f"| Open Skeleton | {_format_metric(current['recall'])} | "
            f"{_format_metric(current['precision'])} | "
            f"{_format_metric(current['evidence_correctness'])} | "
            f"{_format_metric(current['conflict_detection'])} |\n"
        ),
    ]
    if baseline is not None:
        lines.append(
            f"| {baseline['name']} | {_format_metric(baseline['recall'])} | "
            f"{_format_metric(baseline['precision'])} | "
            f"{_format_metric(baseline['evidence_correctness'])} | "
            f"{_format_metric(baseline['conflict_detection'])} |\n"
        )
    lines.extend(
        [
            "\n## Open Skeleton measurements\n\n",
            "| Measurement | Value |\n|---|---:|\n",
        ]
    )
    for key, value in current["measurements"].items():
        lines.append(f"| `{key}` | {value:,} |\n" if isinstance(value, int) else f"| `{key}` | {value} |\n")
    lines.extend(["\n## Claim results\n\n", "| Gold claim | Area | Outcome | Evidence |\n", "|---|---|---|---|\n"])
    for row in current["claims"]:
        lines.append(
            f"| `{row['id']}` | {row['area']} | {row['outcome']} | "
            f"{'correct' if row['evidence_correct'] else 'missing/incorrect'} |\n"
        )
    lines.extend(["\n## Limitations\n\n"])
    lines.extend(f"- {item}\n" for item in result["limitations"])
    path.write_text("".join(lines), encoding="utf-8")
