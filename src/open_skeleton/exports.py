# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from open_skeleton.models import AnalysisResult, Snapshot


def _atomic_write(path: Path, chunks: Iterable[str]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        ) as handle:
            temporary_name = handle.name
            for chunk in chunks:
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def export_jsonl(snapshot: Snapshot, path: Path) -> None:
    def records() -> Iterable[str]:
        yield json.dumps({"record_type": "snapshot", **snapshot.summary()}, sort_keys=True) + "\n"
        for item in snapshot.files:
            yield json.dumps(
                {
                    "record_type": "file",
                    "snapshot_id": snapshot.snapshot_id,
                    **item.to_dict(),
                },
                sort_keys=True,
            ) + "\n"
        for item in snapshot.exclusions:
            yield json.dumps(
                {
                    "record_type": "exclusion",
                    "snapshot_id": snapshot.snapshot_id,
                    **item.to_dict(),
                },
                sort_keys=True,
            ) + "\n"

    _atomic_write(path, records())


def export_markdown(snapshot: Snapshot, path: Path) -> None:
    languages = Counter(item.language for item in snapshot.files)
    roles = Counter(item.role for item in snapshot.files)

    def lines() -> Iterable[str]:
        yield "# Repository Inventory\n\n"
        yield f"- Snapshot: `{snapshot.snapshot_id}`\n"
        yield f"- Root: `{snapshot.root}`\n"
        yield f"- Policy: `{snapshot.policy_version}`\n"
        yield f"- Included files: {len(snapshot.files):,}\n"
        yield f"- Excluded entries: {len(snapshot.exclusions):,}\n"
        yield f"- Included bytes: {snapshot.total_bytes:,}\n"
        yield f"- Included lines: {snapshot.total_lines:,}\n"
        yield f"- Scan duration: {snapshot.duration_ms:,} ms\n\n"

        yield "## Languages\n\n| Language | Files |\n|---|---:|\n"
        for label, count in sorted(languages.items(), key=lambda item: (-item[1], item[0])):
            yield f"| {label} | {count:,} |\n"

        yield "\n## Roles\n\n| Role | Files |\n|---|---:|\n"
        for label, count in sorted(roles.items(), key=lambda item: (-item[1], item[0])):
            yield f"| {label} | {count:,} |\n"

        yield "\n## Included files\n\n"
        yield "| Path | Language | Role | Lines | Bytes | SHA-256 |\n"
        yield "|---|---|---|---:|---:|---|\n"
        for item in snapshot.files:
            escaped_path = item.path.replace("|", "\\|")
            yield (
                f"| `{escaped_path}` | {item.language} | {item.role} | "
                f"{item.line_count:,} | {item.size_bytes:,} | `{item.sha256[:12]}` |\n"
            )

        yield "\n## Exclusions\n\n"
        if not snapshot.exclusions:
            yield "No entries were excluded.\n"
        else:
            yield "| Path | Reason |\n|---|---|\n"
            for item in snapshot.exclusions:
                escaped_path = item.path.replace("|", "\\|")
                yield f"| `{escaped_path}` | `{item.reason}` |\n"

        yield "\n## Trust boundary\n\n"
        yield (
            "This inventory was produced without executing target code, following symlinks, "
            "contacting a model provider, or intentionally reading excluded secrets and binaries.\n"
        )

    _atomic_write(path, lines())


def export_analysis_jsonl(result: AnalysisResult, path: Path) -> None:
    def records() -> Iterable[str]:
        yield json.dumps(
            {"record_type": "analysis", **result.summary()}, sort_keys=True
        ) + "\n"
        for item in result.coverage:
            yield json.dumps(
                {"record_type": "coverage", **item.to_dict()}, sort_keys=True
            ) + "\n"
        for item in result.symbols:
            yield json.dumps(
                {"record_type": "symbol", **item.to_dict()}, sort_keys=True
            ) + "\n"
        for item in result.edges:
            yield json.dumps(
                {"record_type": "edge", **item.to_dict()}, sort_keys=True
            ) + "\n"
        for item in result.evidence:
            yield json.dumps(
                {"record_type": "evidence", **item.to_dict()}, sort_keys=True
            ) + "\n"
        for item in result.claims:
            yield json.dumps(
                {"record_type": "claim", **item.to_dict()}, sort_keys=True
            ) + "\n"

    _atomic_write(path, records())


def export_analysis_markdown(
    result: AnalysisResult,
    path: Path,
    *,
    max_claims: int = 100,
) -> None:
    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    status_counts = Counter(item.status for item in result.claims)
    category_counts = Counter(item.category for item in result.claims)
    importance_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    ordered_claims = sorted(
        result.claims,
        key=lambda item: (importance_order[item.importance], item.category, item.claim),
    )

    def evidence_label(evidence_id: str) -> str:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            return f"`missing:{evidence_id[:12]}`"
        if evidence.start_line is None:
            return f"`{evidence.path}`"
        end = evidence.end_line or evidence.start_line
        span = str(evidence.start_line) if end == evidence.start_line else f"{evidence.start_line}-{end}"
        return f"`{evidence.path}:{span}`"

    def lines() -> Iterable[str]:
        yield "# Evidence-First Analysis\n\n"
        yield f"- Snapshot: `{result.snapshot_id}`\n"
        yield f"- Pipeline: `{result.analyzer_version}`\n"
        yield f"- Duration: {result.duration_ms:,} ms\n"
        yield f"- Symbols: {len(result.symbols):,}\n"
        yield f"- Relationships: {len(result.edges):,}\n"
        yield f"- Evidence receipts: {len(result.evidence):,}\n"
        yield f"- Atomic claims: {len(result.claims):,}\n\n"

        yield "## Analysis coverage\n\n"
        yield "| Analyzer | Language | Eligible | Analyzed | Failed | Coverage |\n"
        yield "|---|---|---:|---:|---:|---:|\n"
        for item in result.coverage:
            yield (
                f"| `{item.analyzer}` | {item.language} | {item.eligible_files:,} | "
                f"{item.analyzed_files:,} | {item.failed_files:,} | "
                f"{item.coverage_ratio:.1%} |\n"
            )
        for item in result.coverage:
            if item.failures:
                yield f"\nFailures for `{item.analyzer}`:\n\n"
                for failure in item.failures:
                    yield f"- `{failure}`\n"

        yield "\n## Claim status\n\n| Status | Claims |\n|---|---:|\n"
        for label, count in sorted(status_counts.items()):
            yield f"| {label} | {count:,} |\n"

        yield "\n## Claim categories\n\n| Category | Claims |\n|---|---:|\n"
        for label, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0])):
            yield f"| {label} | {count:,} |\n"

        yield "\n## Prioritized findings\n\n"
        for claim in ordered_claims[:max_claims]:
            yield f"### [{claim.status.upper()}] {claim.claim}\n\n"
            yield (
                f"- Category: `{claim.category}`\n"
                f"- Importance: `{claim.importance}`\n"
                f"- Confidence: `{claim.confidence:.2f}`\n"
                f"- Producer: `{claim.produced_by}`\n"
            )
            if claim.supporting_evidence:
                labels = ", ".join(evidence_label(item) for item in claim.supporting_evidence)
                yield f"- Supporting evidence: {labels}\n"
            if claim.contradicting_evidence:
                labels = ", ".join(
                    evidence_label(item) for item in claim.contradicting_evidence
                )
                yield f"- Contradicting evidence: {labels}\n"
            if claim.alternative_hypotheses:
                yield "- Alternative hypotheses:\n"
                for alternative in claim.alternative_hypotheses:
                    yield f"  - {alternative}\n"
            yield "\n"

        omitted = len(ordered_claims) - max_claims
        if omitted > 0:
            yield (
                f"_The concise report omits {omitted:,} lower-priority claims. "
                "Use the JSONL export or query interface for the complete ledger._\n"
            )

        yield "\n## Interpretation boundary\n\n"
        yield (
            "Verified means the stated syntax or relationship is directly supported by the "
            "listed receipt. Inferred claims preserve alternatives. Neither label replaces "
            "runtime validation or engineering review.\n"
        )

    _atomic_write(path, lines())
