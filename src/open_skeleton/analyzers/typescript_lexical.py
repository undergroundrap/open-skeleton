# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

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

ANALYZER_NAME = "typescript-lexical"
ANALYZER_VERSION = "typescript-lexical/v1"
ELIGIBLE_LANGUAGES = frozenset({"JavaScript", "JavaScript JSX", "TypeScript", "TypeScript JSX"})
DECLARATION_KEYWORDS = frozenset({"class", "function", "interface", "type", "enum"})
REACT_HOOKS = frozenset(
    {"useCallback", "useContext", "useEffect", "useMemo", "useReducer", "useRef", "useState"}
)
CLIENT_STORES = frozenset({"localStorage", "sessionStorage"})


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    line: int
    end_line: int


def _tokens(source: str) -> list[Token]:
    """Tokenize enough ECMAScript syntax to make exact, comment-safe inventories.

    This intentionally does not claim to be a TypeScript parser. Its coverage is
    reported as lexical, and claims are restricted to syntax visible in tokens.
    """

    result: list[Token] = []
    index = 0
    line = 1
    length = len(source)
    while index < length:
        character = source[index]
        if character.isspace():
            line += character == "\n"
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            if newline < 0:
                break
            index = newline
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                line += source[index:].count("\n")
                break
            line += source[index : end + 2].count("\n")
            index = end + 2
            continue
        if character in {'"', "'", "`"}:
            quote = character
            start_line = line
            index += 1
            value_start = index
            escaped = False
            pieces: list[str] = []
            while index < length:
                current = source[index]
                if escaped:
                    pieces.append(source[value_start : index - 1])
                    pieces.append(current)
                    value_start = index + 1
                    escaped = False
                    line += current == "\n"
                    index += 1
                    continue
                if current == "\\":
                    escaped = True
                    index += 1
                    continue
                if current == quote:
                    pieces.append(source[value_start:index])
                    index += 1
                    break
                line += current == "\n"
                index += 1
            else:
                pieces.append(source[value_start:index])
            result.append(Token("string", "".join(pieces), start_line, line))
            continue
        if character.isalpha() or character in {"_", "$"}:
            start = index
            while index < length and (source[index].isalnum() or source[index] in {"_", "$"}):
                index += 1
            result.append(Token("identifier", source[start:index], line, line))
            continue
        if character.isdigit():
            start = index
            while index < length and (source[index].isalnum() or source[index] in {".", "_"}):
                index += 1
            result.append(Token("number", source[start:index], line, line))
            continue
        result.append(Token("punctuation", character, line, line))
        index += 1
    return result


def _module_name(path: str) -> str:
    return path.rsplit(".", 1)[0].replace("/", ".")


def _next_token(tokens: list[Token], index: int, value: str | None = None) -> int | None:
    candidate = index + 1
    if candidate >= len(tokens):
        return None
    if value is not None and tokens[candidate].value != value:
        return None
    return candidate


def _call_open_paren(tokens: list[Token], index: int) -> int | None:
    """Return the opening parenthesis for a direct or TS-generic call."""

    candidate = index + 1
    if candidate >= len(tokens):
        return None
    if tokens[candidate].value == "(":
        return candidate
    if tokens[candidate].value != "<":
        return None
    depth = 0
    while candidate < len(tokens):
        value = tokens[candidate].value
        if value == "<":
            depth += 1
        elif value == ">":
            depth -= 1
            if depth == 0:
                following = candidate + 1
                if following < len(tokens) and tokens[following].value == "(":
                    return following
                return None
        candidate += 1
    return None


class TypeScriptLexicalAnalyzer:
    name = ANALYZER_NAME
    version = ANALYZER_VERSION

    def analyze(self, snapshot: Snapshot) -> AnalysisResult:
        started = time.perf_counter()
        created_at = utc_now()
        symbols: list[SymbolRecord] = []
        edges: list[EdgeRecord] = []
        evidence: list[EvidenceRecord] = []
        claims: list[ClaimRecord] = []
        failures: list[str] = []
        source_lines_by_path: dict[str, list[str]] = {}
        eligible = [item for item in snapshot.files if item.language in ELIGIBLE_LANGUAGES]
        analyzed_files = 0

        def add_evidence(
            path: str,
            start_line: int,
            end_line: int,
            symbol: str | None,
            kind: str,
            file_sha256: str,
        ) -> EvidenceRecord:
            evidence_id = stable_id(
                "evidence",
                (
                    snapshot.snapshot_id,
                    path,
                    start_line,
                    end_line,
                    symbol,
                    kind,
                    ANALYZER_VERSION,
                ),
            )
            record = EvidenceRecord(
                evidence_id=evidence_id,
                snapshot_id=snapshot.snapshot_id,
                path=path,
                start_line=start_line,
                end_line=end_line,
                symbol=symbol,
                evidence_kind=kind,
                excerpt_sha256=hashlib.sha256(
                    "".join(source_lines_by_path.get(path, [])[start_line - 1 : end_line]).encode(
                        "utf-8"
                    )
                ).hexdigest()
                if path in source_lines_by_path
                else file_sha256,
                analyzer=ANALYZER_VERSION,
                created_at=created_at,
            )
            evidence.append(record)
            return record

        def add_claim(
            text: str,
            category: str,
            importance: str,
            supporting: list[str],
            path: str,
            *,
            status: str = "verified",
            confidence: float = 1.0,
            alternatives: tuple[str, ...] = (),
        ) -> None:
            claims.append(
                ClaimRecord(
                    claim_id=stable_id(
                        "claim", (snapshot.snapshot_id, category, text, ANALYZER_VERSION)
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    claim=text,
                    category=category,
                    status=status,
                    confidence=confidence,
                    importance=importance,
                    produced_by=ANALYZER_VERSION,
                    created_at=created_at,
                    verified_at=created_at if status == "verified" else None,
                    supporting_evidence=tuple(sorted(set(supporting))),
                    invalidation_keys=(f"file:{path}",),
                    alternative_hypotheses=alternatives,
                )
            )

        for file_record in eligible:
            source_path = snapshot.root / Path(file_record.path)
            try:
                payload = source_path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != file_record.sha256:
                    raise ValueError("content changed after snapshot")
                source = payload.decode("utf-8", errors="strict")
                source_lines_by_path[file_record.path] = source.splitlines(keepends=True)
                file_tokens = _tokens(source)
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                failures.append(f"{file_record.path}: {exc.__class__.__name__}: {exc}")
                continue

            module = _module_name(file_record.path)
            module_id = stable_id(
                "symbol",
                (
                    snapshot.snapshot_id,
                    file_record.path,
                    module,
                    "module",
                    ANALYZER_VERSION,
                ),
            )
            symbols.append(
                SymbolRecord(
                    symbol_id=module_id,
                    snapshot_id=snapshot.snapshot_id,
                    path=file_record.path,
                    qualified_name=module,
                    kind="module",
                    start_line=1,
                    end_line=max(1, file_record.line_count),
                    language=file_record.language,
                    analyzer=ANALYZER_VERSION,
                    metadata={"analysis_level": "lexical"},
                )
            )

            fetch_evidence: list[str] = []
            localhost_evidence: list[str] = []
            hook_evidence: dict[str, list[str]] = {name: [] for name in REACT_HOOKS}
            store_evidence: dict[str, list[str]] = {name: [] for name in CLIENT_STORES}
            test_evidence: list[str] = []

            for index, token in enumerate(file_tokens):
                following = _next_token(file_tokens, index)
                next_value = file_tokens[following].value if following is not None else None

                if token.kind == "identifier" and token.value in DECLARATION_KEYWORDS:
                    name_index = _next_token(file_tokens, index)
                    if token.value == "function" and next_value == "*":
                        name_index = _next_token(file_tokens, name_index or index)
                    if name_index is not None and file_tokens[name_index].kind == "identifier":
                        name_token = file_tokens[name_index]
                        qualified = f"{module}.{name_token.value}"
                        kind = "function" if token.value == "function" else token.value
                        receipt = add_evidence(
                            file_record.path,
                            token.line,
                            name_token.end_line,
                            qualified,
                            "symbol",
                            file_record.sha256,
                        )
                        symbol_id = stable_id(
                            "symbol",
                            (
                                snapshot.snapshot_id,
                                file_record.path,
                                qualified,
                                kind,
                                token.line,
                                ANALYZER_VERSION,
                            ),
                        )
                        symbols.append(
                            SymbolRecord(
                                symbol_id=symbol_id,
                                snapshot_id=snapshot.snapshot_id,
                                path=file_record.path,
                                qualified_name=qualified,
                                kind=kind,
                                start_line=token.line,
                                end_line=name_token.end_line,
                                language=file_record.language,
                                analyzer=ANALYZER_VERSION,
                                metadata={"analysis_level": "lexical"},
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
                                        receipt.evidence_id,
                                        ANALYZER_VERSION,
                                    ),
                                ),
                                snapshot_id=snapshot.snapshot_id,
                                source_symbol_id=module_id,
                                source_path=file_record.path,
                                relationship="contains",
                                target_ref=qualified,
                                target_symbol_id=symbol_id,
                                evidence_id=receipt.evidence_id,
                                analyzer=ANALYZER_VERSION,
                            )
                        )

                if token.value == "import":
                    scan = index + 1
                    target: Token | None = None
                    while scan < len(file_tokens) and file_tokens[scan].line <= token.line + 10:
                        if file_tokens[scan].kind == "string":
                            target = file_tokens[scan]
                            break
                        if file_tokens[scan].value == ";":
                            break
                        scan += 1
                    if target:
                        receipt = add_evidence(
                            file_record.path,
                            token.line,
                            target.end_line,
                            module,
                            "import",
                            file_record.sha256,
                        )
                        edges.append(
                            EdgeRecord(
                                edge_id=stable_id(
                                    "edge",
                                    (
                                        snapshot.snapshot_id,
                                        module_id,
                                        "imports",
                                        target.value,
                                        receipt.evidence_id,
                                        ANALYZER_VERSION,
                                    ),
                                ),
                                snapshot_id=snapshot.snapshot_id,
                                source_symbol_id=module_id,
                                source_path=file_record.path,
                                relationship="imports",
                                target_ref=target.value,
                                target_symbol_id=None,
                                evidence_id=receipt.evidence_id,
                                analyzer=ANALYZER_VERSION,
                            )
                        )

                if token.value == "fetch" and next_value == "(":
                    receipt = add_evidence(
                        file_record.path,
                        token.line,
                        token.end_line,
                        module,
                        "http_client_call",
                        file_record.sha256,
                    )
                    fetch_evidence.append(receipt.evidence_id)
                    edges.append(
                        EdgeRecord(
                            edge_id=stable_id(
                                "edge",
                                (
                                    snapshot.snapshot_id,
                                    module_id,
                                    "calls",
                                    "fetch",
                                    receipt.evidence_id,
                                    ANALYZER_VERSION,
                                ),
                            ),
                            snapshot_id=snapshot.snapshot_id,
                            source_symbol_id=module_id,
                            source_path=file_record.path,
                            relationship="calls",
                            target_ref="fetch",
                            target_symbol_id=None,
                            evidence_id=receipt.evidence_id,
                            analyzer=ANALYZER_VERSION,
                        )
                    )

                if token.kind == "string" and "http://localhost:8000" in token.value:
                    receipt = add_evidence(
                        file_record.path,
                        token.line,
                        token.end_line,
                        module,
                        "hardcoded_endpoint",
                        file_record.sha256,
                    )
                    localhost_evidence.extend(
                        [receipt.evidence_id] * token.value.count("http://localhost:8000")
                    )

                if token.value in REACT_HOOKS and _call_open_paren(file_tokens, index) is not None:
                    receipt = add_evidence(
                        file_record.path,
                        token.line,
                        token.end_line,
                        module,
                        "react_hook_call",
                        file_record.sha256,
                    )
                    hook_evidence[token.value].append(receipt.evidence_id)

                if token.value in CLIENT_STORES and next_value == ".":
                    receipt = add_evidence(
                        file_record.path,
                        token.line,
                        token.end_line,
                        module,
                        "browser_storage_access",
                        file_record.sha256,
                    )
                    store_evidence[token.value].append(receipt.evidence_id)

                if (
                    file_record.role == "test"
                    and token.value in {"describe", "it", "test"}
                    and next_value == "("
                ):
                    receipt = add_evidence(
                        file_record.path,
                        token.line,
                        token.end_line,
                        module,
                        "test_declaration",
                        file_record.sha256,
                    )
                    test_evidence.append(receipt.evidence_id)

            if fetch_evidence:
                add_claim(
                    f"{file_record.path} contains {len(fetch_evidence)} fetch call sites.",
                    "http_client_inventory",
                    "high",
                    fetch_evidence,
                    file_record.path,
                )
            if localhost_evidence:
                add_claim(
                    (
                        f"{file_record.path} contains {len(localhost_evidence)} string-literal "
                        "references to http://localhost:8000."
                    ),
                    "hardcoded_endpoint",
                    "high",
                    localhost_evidence,
                    file_record.path,
                )
            for hook, receipts in hook_evidence.items():
                if receipts:
                    add_claim(
                        f"{file_record.path} calls React hook {hook} {len(receipts)} times.",
                        "ui_state",
                        "medium",
                        receipts,
                        file_record.path,
                    )
            for store, receipts in store_evidence.items():
                if receipts:
                    add_claim(
                        f"{file_record.path} accesses {store} at {len(receipts)} call sites.",
                        "browser_storage",
                        "medium",
                        receipts,
                        file_record.path,
                    )
            if test_evidence:
                add_claim(
                    f"{file_record.path} declares {len(test_evidence)} JavaScript test blocks.",
                    "testing",
                    "medium",
                    test_evidence,
                    file_record.path,
                )
            analyzed_files += 1

        coverage = CoverageRecord(
            analyzer=ANALYZER_VERSION,
            language="JavaScript/TypeScript",
            eligible_files=len(eligible),
            analyzed_files=analyzed_files,
            failed_files=len(failures),
            unsupported_files=0,
            failures=tuple(failures),
        )
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
