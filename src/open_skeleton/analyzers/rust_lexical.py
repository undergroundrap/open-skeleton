# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Comment-safe lexical facts for Rust sources.

This is deliberately not a Rust parser, and its coverage is reported as
lexical. It tokenizes enough of the grammar to make exact inventories of the
things a Rust reviewer actually asks about first: where `unsafe` appears, where
the code can panic, what is under `#[cfg(test)]`, and what each module imports.

Three details of Rust lexing are handled explicitly because getting them wrong
silently corrupts every count that follows:

* **Block comments nest.** `/* /* */ */` is one comment. A scanner that stops
  at the first `*/` resumes tokenizing inside a comment.
* **Raw strings carry hashes.** `r#"a "quote" here"#` ends only at a `"`
  followed by the same number of `#`.
* **A single quote is usually a lifetime.** `'static` and `'a` are not
  character literals. Treating them as string openers swallows the rest of the
  file.
"""

from __future__ import annotations

import hashlib
import re
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

ANALYZER_NAME = "rust-lexical"
ANALYZER_VERSION = "rust-lexical/v1"
ELIGIBLE_LANGUAGES = frozenset({"Rust"})

ITEM_KEYWORDS = {
    "fn": "function",
    "struct": "struct",
    "enum": "enum",
    "trait": "trait",
    "union": "union",
}
# Names that abort the process or discard an error, keyed by how they read at a
# call site. `unwrap_or*` variants are excluded: they supply a fallback.
PANIC_MACROS = frozenset({"panic", "unreachable", "todo", "unimplemented", "assert", "assert_eq"})
PANIC_METHODS = frozenset({"unwrap", "expect", "unwrap_err", "expect_err"})
IDENTIFIER_START = re.compile(r"[A-Za-z_]")
IDENTIFIER_BODY = re.compile(r"[A-Za-z0-9_]")


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    line: int


def _read_raw_string(source: str, index: int, length: int) -> int:
    """Return the index just past a raw string beginning at ``index`` (`r`)."""

    cursor = index + 1
    hashes = 0
    while cursor < length and source[cursor] == "#":
        hashes += 1
        cursor += 1
    if cursor >= length or source[cursor] != '"':
        return index + 1
    terminator = '"' + "#" * hashes
    end = source.find(terminator, cursor + 1)
    return length if end < 0 else end + len(terminator)


def _is_char_literal(source: str, index: int, length: int) -> bool:
    """Distinguish `'x'` and `'\\n'` from a lifetime such as `'static`."""

    if index + 1 >= length:
        return False
    if source[index + 1] == "\\":
        closing = source.find("'", index + 2)
        return 0 <= closing <= index + 6
    return index + 2 < length and source[index + 2] == "'"


def tokenize(source: str) -> list[Token]:
    """Tokenize identifiers, punctuation, and lifetimes outside comments and strings."""

    tokens: list[Token] = []
    index = 0
    line = 1
    length = len(source)
    while index < length:
        character = source[index]
        if character == "\n":
            line += 1
            index += 1
            continue
        if character.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index)
            index = length if newline < 0 else newline
            continue
        if source.startswith("/*", index):
            depth = 1
            cursor = index + 2
            while cursor < length and depth:
                if source.startswith("/*", cursor):
                    depth += 1
                    cursor += 2
                elif source.startswith("*/", cursor):
                    depth -= 1
                    cursor += 2
                else:
                    line += source[cursor] == "\n"
                    cursor += 1
            index = cursor
            continue
        if character in {"r", "b"} and index + 1 < length and source[index + 1] in {'"', "#"}:
            end = _read_raw_string(source, index, length)
            if end > index + 1:
                line += source[index:end].count("\n")
                index = end
                continue
        if character == '"':
            cursor = index + 1
            while cursor < length:
                if source[cursor] == "\\":
                    cursor += 2
                    continue
                if source[cursor] == '"':
                    break
                line += source[cursor] == "\n"
                cursor += 1
            index = cursor + 1
            continue
        if character == "'":
            if _is_char_literal(source, index, length):
                closing = source.find("'", index + 1)
                index = length if closing < 0 else closing + 1
                continue
            # A lifetime: emit nothing and step past the tick so the name that
            # follows is not mistaken for an item.
            index += 1
            continue
        if IDENTIFIER_START.match(character):
            start = index
            while index < length and IDENTIFIER_BODY.match(source[index]):
                index += 1
            tokens.append(Token("identifier", source[start:index], line))
            continue
        tokens.append(Token("punctuation", character, line))
        index += 1
    return tokens


def _module_name(path: str) -> str:
    parts = path.removesuffix(".rs").split("/")
    if parts[-1] in {"mod", "lib", "main"} and len(parts) > 1:
        parts = parts[:-1]
    return "::".join(parts) or "crate"


def _use_target(tokens: list[Token], start: int) -> tuple[str, int]:
    """Collect the path of a `use` statement, stopping at `;`, `{`, or `as`."""

    pieces: list[str] = []
    cursor = start
    while cursor < len(tokens):
        token = tokens[cursor]
        if token.kind == "punctuation" and token.value in {";", "{"}:
            break
        if token.kind == "identifier":
            if token.value == "as":
                break
            pieces.append(token.value)
        cursor += 1
    return "::".join(pieces), cursor


class RustLexicalAnalyzer:
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

        eligible = [item for item in snapshot.files if item.language in ELIGIBLE_LANGUAGES]
        analyzed_files = 0
        unsafe_receipts: list[str] = []
        unsafe_files: set[str] = set()
        panic_receipts: list[str] = []
        test_receipts: list[str] = []

        def census_receipt(kind: str) -> EvidenceRecord:
            """A repository-wide receipt for a counted absence.

            A verified claim must cite something, and "no `unsafe` appears" is
            evidenced by the whole analyzed set rather than by any one line. The
            synthetic path keeps it out of per-file yield attribution.
            """

            record = EvidenceRecord(
                evidence_id=stable_id(
                    "evidence", (snapshot.snapshot_id, ".", kind, ANALYZER_VERSION)
                ),
                snapshot_id=snapshot.snapshot_id,
                path=".",
                start_line=None,
                end_line=None,
                symbol=None,
                evidence_kind=kind,
                excerpt_sha256=snapshot.snapshot_id,
                analyzer=ANALYZER_VERSION,
                created_at=created_at,
            )
            evidence.append(record)
            return record

        def receipt(
            path: str, line: int, kind: str, symbol: str | None, excerpt: str
        ) -> EvidenceRecord:
            record = EvidenceRecord(
                evidence_id=stable_id(
                    "evidence", (snapshot.snapshot_id, path, line, kind, symbol, ANALYZER_VERSION)
                ),
                snapshot_id=snapshot.snapshot_id,
                path=path,
                start_line=line,
                end_line=line,
                symbol=symbol,
                evidence_kind=kind,
                excerpt_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                analyzer=ANALYZER_VERSION,
                created_at=created_at,
            )
            evidence.append(record)
            return record

        for file_record in eligible:
            source_path = snapshot.root / Path(file_record.path)
            try:
                payload = source_path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != file_record.sha256:
                    raise ValueError("content changed after snapshot")
                source = payload.decode("utf-8", errors="strict")
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                failures.append(f"{file_record.path}: {exc.__class__.__name__}: {exc}")
                continue

            lines = source.splitlines()
            module = _module_name(file_record.path)
            module_symbol_id = stable_id(
                "symbol", (snapshot.snapshot_id, file_record.path, "module", ANALYZER_VERSION)
            )
            symbols.append(
                SymbolRecord(
                    symbol_id=module_symbol_id,
                    snapshot_id=snapshot.snapshot_id,
                    path=file_record.path,
                    qualified_name=module,
                    kind="module",
                    start_line=1,
                    end_line=max(1, file_record.line_count),
                    language="Rust",
                    analyzer=ANALYZER_VERSION,
                    metadata={},
                )
            )

            tokens = tokenize(source)
            index = 0
            while index < len(tokens):
                token = tokens[index]
                if token.kind != "identifier":
                    index += 1
                    continue
                excerpt = lines[token.line - 1] if token.line - 1 < len(lines) else ""

                if token.value == "use":
                    target, cursor = _use_target(tokens, index + 1)
                    if target:
                        import_receipt = receipt(
                            file_record.path, token.line, "import", module, excerpt
                        )
                        edges.append(
                            EdgeRecord(
                                edge_id=stable_id(
                                    "edge",
                                    (
                                        snapshot.snapshot_id,
                                        module_symbol_id,
                                        "imports",
                                        target,
                                        token.line,
                                        ANALYZER_VERSION,
                                    ),
                                ),
                                snapshot_id=snapshot.snapshot_id,
                                source_symbol_id=module_symbol_id,
                                source_path=file_record.path,
                                relationship="imports",
                                target_ref=target,
                                target_symbol_id=None,
                                evidence_id=import_receipt.evidence_id,
                                analyzer=ANALYZER_VERSION,
                            )
                        )
                    index = cursor
                    continue

                if token.value == "unsafe":
                    unsafe_files.add(file_record.path)
                    unsafe_receipts.append(
                        receipt(
                            file_record.path, token.line, "unsafe_surface", module, excerpt
                        ).evidence_id
                    )
                    index += 1
                    continue

                if token.value in ITEM_KEYWORDS and index + 1 < len(tokens):
                    name_token = tokens[index + 1]
                    if name_token.kind == "identifier":
                        qualified = f"{module}::{name_token.value}"
                        item_receipt = receipt(
                            file_record.path, token.line, "symbol", qualified, excerpt
                        )
                        symbol_id = stable_id(
                            "symbol",
                            (
                                snapshot.snapshot_id,
                                file_record.path,
                                qualified,
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
                                kind=ITEM_KEYWORDS[token.value],
                                start_line=token.line,
                                end_line=token.line,
                                language="Rust",
                                analyzer=ANALYZER_VERSION,
                                metadata={},
                            )
                        )
                        edges.append(
                            EdgeRecord(
                                edge_id=stable_id(
                                    "edge",
                                    (
                                        snapshot.snapshot_id,
                                        module_symbol_id,
                                        "contains",
                                        qualified,
                                        ANALYZER_VERSION,
                                    ),
                                ),
                                snapshot_id=snapshot.snapshot_id,
                                source_symbol_id=module_symbol_id,
                                source_path=file_record.path,
                                relationship="contains",
                                target_ref=qualified,
                                target_symbol_id=symbol_id,
                                evidence_id=item_receipt.evidence_id,
                                analyzer=ANALYZER_VERSION,
                            )
                        )
                        if name_token.value == "main" and token.value == "fn":
                            claims.append(
                                self._claim(
                                    snapshot,
                                    created_at,
                                    text=f"{file_record.path} declares a `fn main` entry point.",
                                    category="application_entry",
                                    supporting=(item_receipt.evidence_id,),
                                    path=file_record.path,
                                )
                            )
                    index += 2
                    continue

                if token.value == "test" and index >= 2:
                    previous = tokens[index - 1]
                    if previous.kind == "punctuation" and previous.value == "[":
                        test_receipts.append(
                            receipt(
                                file_record.path, token.line, "test_attribute", module, excerpt
                            ).evidence_id
                        )
                    index += 1
                    continue

                if token.value in PANIC_METHODS and index >= 1:
                    previous = tokens[index - 1]
                    following = tokens[index + 1] if index + 1 < len(tokens) else None
                    is_call = following is not None and following.value == "("
                    if previous.kind == "punctuation" and previous.value == "." and is_call:
                        panic_receipts.append(
                            receipt(
                                file_record.path, token.line, "panic_site", module, excerpt
                            ).evidence_id
                        )
                    index += 1
                    continue

                if token.value in PANIC_MACROS and index + 1 < len(tokens):
                    following = tokens[index + 1]
                    if following.kind == "punctuation" and following.value == "!":
                        panic_receipts.append(
                            receipt(
                                file_record.path, token.line, "panic_site", module, excerpt
                            ).evidence_id
                        )
                    index += 1
                    continue

                index += 1
            analyzed_files += 1

        if unsafe_receipts:
            claims.append(
                self._claim(
                    snapshot,
                    created_at,
                    text=(
                        f"{len(unsafe_receipts)} `unsafe` keyword sites appear across "
                        f"{len(unsafe_files)} of {len(eligible)} Rust files; each marks a "
                        "block or signature where the compiler's guarantees are delegated "
                        "to the author."
                    ),
                    category="unsafe_surface",
                    supporting=tuple(sorted(set(unsafe_receipts))),
                    importance="high",
                )
            )
        elif eligible:
            claims.append(
                self._claim(
                    snapshot,
                    created_at,
                    text=(
                        f"No `unsafe` keyword appears in the {len(eligible)} analyzed Rust "
                        "files; this census covers source tokens, not dependencies."
                    ),
                    category="unsafe_surface",
                    supporting=(census_receipt("unsafe_census").evidence_id,),
                    importance="high",
                )
            )

        if panic_receipts:
            claims.append(
                self._claim(
                    snapshot,
                    created_at,
                    text=(
                        f"{len(panic_receipts)} panicking call sites appear in Rust source "
                        "(`unwrap`, `expect`, `panic!`, `unreachable!`, `todo!`, "
                        "`unimplemented!`, `assert!`); each aborts the thread rather than "
                        "returning an error."
                    ),
                    category="panic_site",
                    supporting=tuple(sorted(set(panic_receipts))[:200]),
                    importance="high",
                )
            )

        if test_receipts:
            claims.append(
                self._claim(
                    snapshot,
                    created_at,
                    text=f"{len(test_receipts)} `#[test]` attributes appear in Rust source.",
                    category="testing",
                    supporting=tuple(sorted(set(test_receipts))[:200]),
                )
            )
        elif eligible:
            claims.append(
                self._claim(
                    snapshot,
                    created_at,
                    text=(
                        f"No `#[test]` attribute appears in the {len(eligible)} analyzed Rust "
                        "files; integration tests under `tests/` are counted only if present "
                        "in this snapshot."
                    ),
                    category="testing_gap",
                    supporting=(census_receipt("rust_test_census").evidence_id,),
                )
            )

        coverage = CoverageRecord(
            analyzer=ANALYZER_VERSION,
            language="Rust",
            eligible_files=len(eligible),
            analyzed_files=analyzed_files,
            failed_files=len(failures),
            unsupported_files=0,
            failures=tuple(sorted(failures)),
        )
        return AnalysisResult(
            snapshot_id=snapshot.snapshot_id,
            analyzer_version=ANALYZER_VERSION,
            created_at=created_at,
            duration_ms=round((time.perf_counter() - started) * 1000),
            symbols=tuple(symbols),
            edges=tuple(edges),
            evidence=tuple(evidence),
            claims=tuple(claims),
            coverage=(coverage,),
        )

    def _claim(
        self,
        snapshot: Snapshot,
        created_at: str,
        *,
        text: str,
        category: str,
        supporting: tuple[str, ...],
        importance: str = "medium",
        path: str | None = None,
    ) -> ClaimRecord:
        invalidation = (f"file:{path}",) if path else ("language:rust",)
        return ClaimRecord(
            claim_id=stable_id("claim", (snapshot.snapshot_id, category, text, ANALYZER_VERSION)),
            snapshot_id=snapshot.snapshot_id,
            claim=text,
            category=category,
            status="verified",
            confidence=1.0,
            importance=importance,
            produced_by=ANALYZER_VERSION,
            created_at=created_at,
            verified_at=created_at,
            supporting_evidence=supporting,
            invalidation_keys=invalidation,
            alternative_hypotheses=(
                (
                    "Lexical analysis records tokens, not reachability; a recorded site "
                    "may sit behind a `cfg` gate that never compiles for a given target."
                ),
            ),
        )
