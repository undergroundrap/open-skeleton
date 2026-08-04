# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

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

ANALYZER_NAME = "hum-semantic-index"
ANALYZER_VERSION = "hum-semantic-index/v1"
SUPPORTED_SCHEMA = "hum.semantic_graph.v0"


def _span_line(value: Any) -> int:
    if isinstance(value, dict):
        line = value.get("line") or value.get("start_line")
        if isinstance(line, int) and line > 0:
            return line
    return 1


def _flatten_symbols(values: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(values, list):
        return result
    for value in values:
        if not isinstance(value, dict):
            continue
        result.append(value)
        result.extend(_flatten_symbols(value.get("children")))
    return result


def _normalize_graph_path(root: Path, value: str) -> str | None:
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(root)
        except ValueError:
            return None
    normalized = candidate.as_posix().removeprefix("./")
    return normalized or None


class HumSemanticIndexAnalyzer:
    name = ANALYZER_NAME
    version = ANALYZER_VERSION

    def __init__(self, index_paths: Sequence[Path] | Path | None = None) -> None:
        """Accept one index or several.

        `hum graph` merges multiple sources into a single document, so one index
        is the common case. Several are accepted because generating a whole-repo
        index can be sharded — by crate, or to stay inside a command-line length
        limit — and a partial index is worse than a split one.
        """

        if index_paths is None:
            supplied: tuple[Path, ...] = ()
        elif isinstance(index_paths, Path):
            supplied = (index_paths,)
        else:
            supplied = tuple(index_paths)
        self.index_paths = tuple(item.expanduser().resolve() for item in supplied)

    def analyze(self, snapshot: Snapshot) -> AnalysisResult:
        started = time.perf_counter()
        created_at = utc_now()
        eligible = [item for item in snapshot.files if item.language == "Hum"]
        if not eligible:
            return self._result(
                snapshot,
                created_at,
                started,
                symbols=(),
                edges=(),
                evidence=(),
                claims=(),
                coverage=CoverageRecord(
                    analyzer=ANALYZER_VERSION,
                    language="Hum",
                    eligible_files=0,
                    analyzed_files=0,
                    failed_files=0,
                    unsupported_files=0,
                ),
            )
        if not self.index_paths:
            limitation = (
                f"{len(eligible)} Hum files require a pre-generated {SUPPORTED_SCHEMA} index; "
                "Open Skeleton did not execute the target compiler. Generate one covering "
                "every Hum file — `hum graph` accepts multiple paths and merges them — then "
                "supply it with --hum-index. Repeat --hum-index to combine sharded indexes."
            )
            return self._result(
                snapshot,
                created_at,
                started,
                symbols=(),
                edges=(),
                evidence=(),
                claims=(),
                coverage=CoverageRecord(
                    analyzer=ANALYZER_VERSION,
                    language="Hum",
                    eligible_files=len(eligible),
                    analyzed_files=0,
                    failed_files=0,
                    unsupported_files=len(eligible),
                    failures=(limitation,),
                ),
            )

        symbols: list[SymbolRecord] = []
        edges: list[EdgeRecord] = []
        evidence: list[EvidenceRecord] = []
        claims: list[ClaimRecord] = []
        failures: list[str] = []
        documents: list[tuple[Path, str, dict[str, Any]]] = []
        for index_path in self.index_paths:
            try:
                payload = index_path.read_bytes()
                document = json.loads(payload)
                if not isinstance(document, dict):
                    raise ValueError("native index must be a JSON object")
                if document.get("schema") != SUPPORTED_SCHEMA:
                    raise ValueError(
                        f"unsupported schema {document.get('schema')!r}; "
                        f"expected {SUPPORTED_SCHEMA}"
                    )
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                failures.append(f"{index_path}: {exc.__class__.__name__}: {exc}")
                continue
            documents.append((index_path, hashlib.sha256(payload).hexdigest(), document))

        if not documents:
            return self._result(
                snapshot,
                created_at,
                started,
                symbols=(),
                edges=(),
                evidence=(),
                claims=(),
                coverage=CoverageRecord(
                    analyzer=ANALYZER_VERSION,
                    language="Hum",
                    eligible_files=len(eligible),
                    analyzed_files=0,
                    failed_files=len(eligible),
                    unsupported_files=0,
                    failures=tuple(failures),
                ),
            )

        # Each supplied index keeps its own receipt and hash, so a claim can be
        # traced back to the exact index that produced it.
        index_receipts: list[EvidenceRecord] = []
        merged_files: list[tuple[str, dict[str, Any]]] = []
        for index_path, digest, document in documents:
            index_receipt = EvidenceRecord(
                evidence_id=stable_id(
                    "evidence",
                    (snapshot.snapshot_id, str(index_path), digest, ANALYZER_VERSION),
                ),
                snapshot_id=snapshot.snapshot_id,
                path=f"@hum-index:{index_path}",
                start_line=None,
                end_line=None,
                symbol=SUPPORTED_SCHEMA,
                evidence_kind="native_semantic_index",
                excerpt_sha256=digest,
                analyzer=ANALYZER_VERSION,
                created_at=created_at,
            )
            evidence.append(index_receipt)
            index_receipts.append(index_receipt)
            graph_files = document.get("files")
            if not isinstance(graph_files, list):
                failures.append(f"{index_path}: field `files` must be an array")
                continue
            merged_files.extend((digest, item) for item in graph_files)

        index_evidence = index_receipts[0]
        index_hash = documents[0][1]
        files_by_path = {item.path: item for item in eligible}
        analyzed_paths: set[str] = set()

        for index_hash, graph_file in merged_files:
            if not isinstance(graph_file, dict) or not isinstance(graph_file.get("path"), str):
                failures.append("native index contains a file entry without a string path")
                continue
            path = _normalize_graph_path(snapshot.root, graph_file["path"])
            if path in analyzed_paths:
                continue
            if path is None or path not in files_by_path:
                failures.append(
                    f"native index path is outside or absent from snapshot: {graph_file['path']}"
                )
                continue
            file_record = files_by_path[path]
            source_path = snapshot.root / Path(path)
            try:
                source_payload = source_path.read_bytes()
                if hashlib.sha256(source_payload).hexdigest() != file_record.sha256:
                    raise ValueError("content changed after snapshot")
                source_lines = source_payload.decode("utf-8", errors="strict").splitlines(
                    keepends=True
                )
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                failures.append(f"{path}: {exc.__class__.__name__}: {exc}")
                continue

            analyzed_paths.add(path)
            module = graph_file.get("module") or Path(path).stem
            module_id = stable_id(
                "symbol",
                (snapshot.snapshot_id, path, module, "module", ANALYZER_VERSION),
            )
            symbols.append(
                SymbolRecord(
                    symbol_id=module_id,
                    snapshot_id=snapshot.snapshot_id,
                    path=path,
                    qualified_name=str(module),
                    kind="module",
                    start_line=1,
                    end_line=max(1, file_record.line_count),
                    language="Hum",
                    analyzer=ANALYZER_VERSION,
                    metadata={"native_schema": SUPPORTED_SCHEMA, "index_sha256": index_hash},
                )
            )
            for native_symbol in _flatten_symbols(graph_file.get("symbols")):
                name = native_symbol.get("name")
                kind = native_symbol.get("kind")
                if not isinstance(name, str) or not isinstance(kind, str):
                    continue
                line = min(max(1, _span_line(native_symbol.get("span"))), max(1, len(source_lines)))
                qualified = f"{module}.{name}"
                source_hash = hashlib.sha256(source_lines[line - 1].encode("utf-8")).hexdigest()
                receipt_id = stable_id(
                    "evidence",
                    (snapshot.snapshot_id, path, line, qualified, ANALYZER_VERSION),
                )
                evidence.append(
                    EvidenceRecord(
                        evidence_id=receipt_id,
                        snapshot_id=snapshot.snapshot_id,
                        path=path,
                        start_line=line,
                        end_line=line,
                        symbol=qualified,
                        evidence_kind="native_symbol",
                        excerpt_sha256=source_hash,
                        analyzer=ANALYZER_VERSION,
                        created_at=created_at,
                    )
                )
                symbol_id = stable_id(
                    "symbol",
                    (snapshot.snapshot_id, path, qualified, kind, line, ANALYZER_VERSION),
                )
                symbols.append(
                    SymbolRecord(
                        symbol_id=symbol_id,
                        snapshot_id=snapshot.snapshot_id,
                        path=path,
                        qualified_name=qualified,
                        kind=kind,
                        start_line=line,
                        end_line=line,
                        language="Hum",
                        analyzer=ANALYZER_VERSION,
                        metadata={
                            "native_id": native_symbol.get("id"),
                            "native_schema": SUPPORTED_SCHEMA,
                            "index_sha256": index_hash,
                        },
                    )
                )
                edges.append(
                    EdgeRecord(
                        edge_id=stable_id(
                            "edge",
                            (
                                snapshot.snapshot_id,
                                module_id,
                                "contains",
                                qualified,
                                receipt_id,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        source_symbol_id=module_id,
                        source_path=path,
                        relationship="contains",
                        target_ref=qualified,
                        target_symbol_id=symbol_id,
                        evidence_id=receipt_id,
                        analyzer=ANALYZER_VERSION,
                    )
                )

        raw_summary = document.get("summary")
        summary: dict[str, Any] = raw_summary if isinstance(raw_summary, dict) else {}
        text = (
            f"The supplied {SUPPORTED_SCHEMA} index reports {summary.get('items', 0)} items, "
            f"{summary.get('tasks', 0)} tasks, {summary.get('tests', 0)} tests, and "
            f"{summary.get('errors', 0)} errors."
        )
        claims.append(
            ClaimRecord(
                claim_id=stable_id(
                    "claim",
                    (
                        snapshot.snapshot_id,
                        "hum_native_summary",
                        text,
                        index_hash,
                        ANALYZER_VERSION,
                    ),
                ),
                snapshot_id=snapshot.snapshot_id,
                claim=text,
                category="hum_native_summary",
                status="verified",
                confidence=1.0,
                importance="high",
                produced_by=ANALYZER_VERSION,
                created_at=created_at,
                verified_at=created_at,
                supporting_evidence=(index_evidence.evidence_id,),
                invalidation_keys=(f"hum:index:{index_hash}",),
                alternative_hypotheses=(
                    (
                        "The index is content-pinned but hum.semantic_graph.v0 does not embed source "
                        "file hashes, so freshness is the caller's responsibility."
                    ),
                ),
            )
        )
        unsupported = len(eligible) - len(analyzed_paths)
        if unsupported:
            failures.append(
                f"native index omitted {unsupported} of {len(eligible)} Hum files in the snapshot"
            )
        coverage = CoverageRecord(
            analyzer=ANALYZER_VERSION,
            language="Hum",
            eligible_files=len(eligible),
            analyzed_files=len(analyzed_paths),
            failed_files=0,
            unsupported_files=unsupported,
            failures=tuple(failures),
        )
        return self._result(
            snapshot,
            created_at,
            started,
            symbols=tuple(symbols),
            edges=tuple(edges),
            evidence=tuple(evidence),
            claims=tuple(claims),
            coverage=coverage,
        )

    def _result(
        self,
        snapshot: Snapshot,
        created_at: str,
        started: float,
        *,
        symbols: tuple[SymbolRecord, ...],
        edges: tuple[EdgeRecord, ...],
        evidence: tuple[EvidenceRecord, ...],
        claims: tuple[ClaimRecord, ...],
        coverage: CoverageRecord,
    ) -> AnalysisResult:
        return AnalysisResult(
            snapshot_id=snapshot.snapshot_id,
            analyzer_version=ANALYZER_VERSION,
            created_at=created_at,
            duration_ms=round((time.perf_counter() - started) * 1000),
            symbols=tuple(sorted(symbols, key=lambda item: (item.path, item.start_line))),
            edges=tuple(sorted(edges, key=lambda item: item.edge_id)),
            evidence=tuple(sorted(evidence, key=lambda item: item.evidence_id)),
            claims=tuple(sorted(claims, key=lambda item: item.claim_id)),
            coverage=(coverage,),
        )
