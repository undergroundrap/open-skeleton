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
HTTP_METHOD_NAMES = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})
# Calls whose first string argument is a served path. `nest` and `scope` mount a
# sub-router under a prefix, which is a mount point rather than an endpoint.
MOUNT_BUILDERS = frozenset({"nest", "scope", "nest_service"})
ROUTE_BUILDERS = frozenset({"route", "resource"}) | MOUNT_BUILDERS
# `get("/x")` means a request only if one of these is in scope; otherwise it is
# axum's method router and reading it as a call would invent outbound traffic.
HTTP_CLIENT_CRATES = frozenset({"reqwest", "ureq", "surf", "isahc", "hyper"})
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
                opening = source.find('"', index)
                body = source[opening + 1 : end].rstrip("#").rstrip('"')
                tokens.append(Token("string", body, line))
                line += source[index:end].count("\n")
                index = end
                continue
        if character == '"':
            cursor = index + 1
            start_line = line
            while cursor < length:
                if source[cursor] == "\\":
                    cursor += 2
                    continue
                if source[cursor] == '"':
                    break
                line += source[cursor] == "\n"
                cursor += 1
            # The contents are kept as a `string` token rather than discarded.
            # Consumers that walk identifiers filter on kind and are unaffected,
            # but a route path only exists inside a literal, so throwing the
            # literal away made every served path unreachable by construction.
            tokens.append(Token("string", source[index + 1 : cursor], start_line))
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


def _is_macro_parameter(tokens: list[Token], index: int) -> bool:
    """Whether an identifier is a `macro_rules!` substitution rather than a name.

    `impl fmt::Display for $name` inside a macro declares nothing about a type
    called `name`; it is a template. The tokenizer emits `$` separately, so the
    preceding token is the whole test — and without it the analyzer reports a
    macro parameter as a real type, which is a fabricated fact rather than a
    missed one.
    """

    return index > 0 and tokens[index - 1].value == "$"


def _name_index(tokens: list[Token]) -> dict[str, int]:
    """Every identifier a Rust file mentions, with the line it first appears on.

    The same concordance built for Python and TypeScript. Rust contributed
    nothing to it, which is a large part of why a Rust repository looked empty
    beside a Python one.
    """

    found: dict[str, int] = {}
    for token in tokens:
        if token.kind != "identifier" or len(token.value) < 3:
            continue
        found[token.value] = min(found.get(token.value, token.line), token.line)
    return found


def _constants(tokens: list[Token]) -> dict[str, dict[str, Any]]:
    """`const` and `static` items with their declared type and value.

    These are Rust's tunables. A timeout, a capacity or a scaling factor is
    written here exactly as it is written at Python module scope, and neither
    the type nor the value was being recorded.
    """

    found: dict[str, dict[str, Any]] = {}
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value not in {"const", "static"}:
            continue
        step = index + 1
        if step < total and tokens[step].value == "mut":
            step += 1
        if step >= total or tokens[step].kind != "identifier":
            continue
        name = tokens[step].value
        # `const NAME: Type = value;`
        declared = ""
        value = ""
        scan = step + 1
        if scan < total and tokens[scan].value == ":":
            scan += 1
            parts: list[str] = []
            # A fixed-length array type carries its own semicolon: `[&str; 2]`.
            # Stopping at the first one truncates the type and loses the value.
            depth = 0
            while scan < total:
                current = tokens[scan].value
                if current in {"[", "(", "<"}:
                    depth += 1
                elif current in {"]", ")", ">"}:
                    depth -= 1
                elif depth <= 0 and current in {"=", ";"}:
                    break
                parts.append(current)
                scan += 1
            declared = "".join(parts)
        if scan < total and tokens[scan].value == "=":
            scan += 1
            parts = []
            while scan < total and tokens[scan].value != ";":
                parts.append(tokens[scan].value)
                scan += 1
            # The tokenizer emits every non-identifier character separately,
            # so a literal like 22.0 arrives as four tokens. Joining with a
            # space renders it "2 2 . 0"; the value has to be reassembled.
            reassembled = "".join(parts)[:60]
            # The tokenizer discards string contents so comments and quotes
            # cannot corrupt the scan, which means a string constant's value is
            # simply not recoverable here. What survives is punctuation — `[,]`
            # for a string array — and printing that would be worse than
            # printing nothing, so a value with no alphanumeric content is
            # omitted and the constant is reported by name, type and site.
            value = reassembled if any(ch.isalnum() for ch in reassembled) else ""
        entry: dict[str, Any] = {"line": token.line, "kind": token.value}
        if declared:
            entry["type"] = declared
        if value:
            entry["value"] = value
        found.setdefault(name, entry)
    return found


def _struct_fields(tokens: list[Token]) -> dict[str, dict[str, Any]]:
    """Fields of each struct: the data contract, as Rust writes it down.

    A struct is where a Rust program states what it stores, the way an
    annotated class is in Python. Reporting the struct name without its fields
    describes a container by its label alone.
    """

    found: dict[str, dict[str, Any]] = {}
    total = len(tokens)
    index = 0
    while index < total:
        token = tokens[index]
        if token.kind != "identifier" or token.value != "struct":
            index += 1
            continue
        if index + 1 >= total or tokens[index + 1].kind != "identifier":
            index += 1
            continue
        name = tokens[index + 1].value
        # Skip generics and any where-clause to reach the body.
        scan = index + 2
        depth = 0
        while scan < total and tokens[scan].value not in {"{", ";"}:
            scan += 1
        if scan >= total or tokens[scan].value == ";":
            index = scan + 1
            continue
        scan += 1
        depth = 1
        fields: list[dict[str, Any]] = []
        while scan < total and depth:
            current = tokens[scan]
            if current.value in {"{", "(", "["}:
                depth += 1
            elif current.value in {"}", ")", "]"}:
                depth -= 1
                if depth == 0:
                    break
            elif (
                depth == 1
                and current.kind == "identifier"
                and current.value not in {"pub", "crate", "super", "mut", "ref", "dyn"}
                and scan + 1 < total
                and tokens[scan + 1].value == ":"
            ):
                parts = []
                inner = scan + 2
                nested = 0
                while inner < total:
                    value = tokens[inner].value
                    if value in {"<", "(", "["}:
                        nested += 1
                    elif value in {">", ")", "]"}:
                        nested -= 1
                    elif value in {",", "}"} and nested <= 0:
                        # A comma at the field's own depth ends it; a brace
                        # ends the struct body and the last field with it.
                        break
                    parts.append(value)
                    inner += 1
                fields.append(
                    {
                        "name": current.value,
                        # The key is `annotation` to match the Python analyzer:
                        # one shape for declared model fields means one panel
                        # renders both languages rather than each growing its
                        # own. A struct field has no default, so it is always
                        # required in a struct literal.
                        "annotation": "".join(parts)[:80],
                        "required": True,
                        "line": current.line,
                    }
                )
                scan = inner
                continue
            scan += 1
        if fields:
            found[name] = {"fields": fields, "line": token.line, "bases": []}
        index = scan + 1
    return found


def _mutable_statics(tokens: list[Token]) -> list[tuple[str, int, str]]:
    """Statics that outlive a call, as `(name, line, why it is shared)`.

    Rust's answer to a module-level dict is a `static`. A `static mut` is
    process-local state that every thread shares with no synchronisation at
    all, and an interior-mutability wrapper is the same state with a lock
    around it — the sharing is the property worth reporting either way, since
    a second process of this program observes none of it.
    """

    shared_wrappers = {"Mutex", "RwLock", "RefCell", "Cell", "OnceLock", "OnceCell", "Lazy"}
    found: list[tuple[str, int, str]] = []
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "static":
            continue
        step = index + 1
        mutable = step < total and tokens[step].value == "mut"
        if mutable:
            step += 1
        if step >= total or tokens[step].kind != "identifier":
            continue
        name = tokens[step].value
        wrapper = ""
        scan = step
        while scan < total and scan < step + 40 and tokens[scan].value != ";":
            if tokens[scan].kind == "identifier" and tokens[scan].value in shared_wrappers:
                wrapper = tokens[scan].value
                break
            scan += 1
        if mutable:
            found.append(
                (
                    name,
                    token.line,
                    "declared `static mut`, so every thread shares it with no synchronisation",
                )
            )
        elif wrapper:
            found.append(
                (
                    name,
                    token.line,
                    f"a `static` holding `{wrapper}`, so its contents change while the process runs",
                )
            )
    return found


def _error_surface(tokens: list[Token]) -> dict[str, Any]:
    """How this file propagates failure: `Result` signatures and `?` sites.

    Rust states its failure surface in the type system, which is the opposite
    of a language that raises. A function returning `Result` is declaring that
    it can fail and that its caller must decide what to do; a `?` is a caller
    declining to and passing it upward. Counting both says where errors are
    handled and where they only travel.

    The error types named in those signatures are the vocabulary a caller
    codes against, so they are collected rather than only counted.
    """

    total = len(tokens)
    fallible: list[tuple[str, int]] = []
    error_types: dict[str, int] = {}
    propagations = 0

    for index, token in enumerate(tokens):
        if token.kind == "punctuation" and token.value == "?":
            # `?` after an identifier or a closing bracket is propagation. In
            # any other position it is part of a type or a macro.
            previous = tokens[index - 1] if index else None
            if previous is not None and (
                previous.kind == "identifier" or previous.value in {")", "]", "}"}
            ):
                propagations += 1
            continue
        if token.kind != "identifier" or token.value != "fn":
            continue
        if index + 1 >= total or tokens[index + 1].kind != "identifier":
            continue
        name = tokens[index + 1].value
        # Walk the signature to its body, watching for a `Result` return.
        scan = index + 2
        depth = 0
        while scan < total and scan < index + 200:
            current = tokens[scan]
            if current.value in {"(", "<", "["}:
                depth += 1
            elif current.value in {")", ">", "]"}:
                depth -= 1
            elif current.value == "{" and depth <= 0:
                break
            elif current.kind == "identifier" and current.value in {"Result", "Option"}:
                fallible.append((name, token.line))
                # The error parameter is whatever follows the comma inside
                # `Result<T, E>`; a type alias has none and is still a Result.
                inner = scan + 1
                nested = 0
                seen_comma = False
                while inner < total and inner < scan + 60:
                    value = tokens[inner].value
                    if value in {"<", "(", "["}:
                        nested += 1
                    elif value in {">", ")", "]"}:
                        nested -= 1
                        if nested <= 0:
                            break
                    elif value == "," and nested == 1:
                        seen_comma = True
                    elif seen_comma and tokens[inner].kind == "identifier" and nested == 1:
                        error_types[tokens[inner].value] = (
                            error_types.get(tokens[inner].value, 0) + 1
                        )
                        break
                    inner += 1
                break
            scan += 1

    return {
        "fallible_functions": fallible,
        "error_types": error_types,
        "propagation_sites": propagations,
    }


def _trait_implementations(tokens: list[Token]) -> list[tuple[str, str, int]]:
    """`impl Trait for Type` pairs: the contracts a type actually satisfies.

    A trait implementation is where a Rust type declares it can be used a
    certain way, which is the closest thing the language has to the capability
    a route exposes in a service. An inherent `impl Type` block declares no
    contract and is excluded.
    """

    found: list[tuple[str, str, int]] = []
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "impl":
            continue
        header: list[str] = []
        scan = index + 1
        # Identifiers inside angle brackets are type arguments, not the trait:
        # `impl From<Error> for BootError` implements From, and reading the
        # last name before `for` would call it Error. A path-qualified trait
        # such as `std::fmt::Display` still resolves to its final segment.
        generics = 0
        while scan < total and tokens[scan].value not in {"{", ";"}:
            value = tokens[scan].value
            if value == "<":
                generics += 1
            elif value == ">":
                generics -= 1
            elif tokens[scan].kind == "identifier" and generics <= 0:
                if _is_macro_parameter(tokens, scan):
                    header.append("$")
                else:
                    header.append(value)
            scan += 1
        if "for" not in header:
            continue
        divider = header.index("for")
        trait_name = header[divider - 1] if divider else ""
        owner = header[divider + 1] if divider + 1 < len(header) else ""
        if trait_name and owner and "$" not in {trait_name, owner}:
            found.append((owner, trait_name, token.line))
    return found


def _http_routes(tokens: list[Token]) -> list[tuple[str, str, int]]:
    """Served paths and their methods, as `(method, path, line)`.

    Two forms cover the common Rust web frameworks. A builder call names the
    path and its handlers together, as in `.route("/x", get(h).post(h2))`, and
    an attribute macro puts the method in the attribute itself, as in
    `#[get("/x")]`. Both are recognised; `nest`/`scope` are recorded as mounts
    because a prefix is not itself a served path.

    A path built from a variable — `.route($path, ...)` inside a macro, or a
    `format!` call — yields no string token, so nothing is recorded. That is
    the intended outcome: the value is not knowable without running the
    program, and naming a guess would be a fabrication.
    """

    found: list[tuple[str, str, int]] = []
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier":
            continue
        value = token.value

        # `#[get("/path")]` — the attribute macro form used by actix and rocket.
        if (
            value in HTTP_METHOD_NAMES
            and index >= 2
            and tokens[index - 2].value == "#"
            and tokens[index - 1].value == "["
            and index + 2 < total
            and tokens[index + 1].value == "("
            and tokens[index + 2].kind == "string"
        ):
            found.append((value.upper(), tokens[index + 2].value, token.line))
            continue

        if value not in ROUTE_BUILDERS or index + 2 >= total:
            continue
        if tokens[index + 1].value != "(" or tokens[index + 2].kind != "string":
            continue
        path = tokens[index + 2].value
        if value in MOUNT_BUILDERS:
            found.append(("MOUNT", path, token.line))
            continue
        # Collect every method named inside this call: axum chains them onto
        # one path, so `get(h).post(h2)` serves two.
        methods: list[str] = []
        depth = 1
        scan = index + 3
        while scan < total and depth:
            current = tokens[scan]
            if current.value == "(":
                depth += 1
            elif current.value == ")":
                depth -= 1
            elif current.kind == "identifier" and current.value in HTTP_METHOD_NAMES:
                methods.append(current.value.upper())
            scan += 1
        for method in methods or ["ANY"]:
            found.append((method, path, token.line))
    return found


def _client_calls(tokens: list[Token]) -> list[tuple[str, str, int]]:
    """Outbound HTTP requests this module makes, as `(method, target, line)`.

    The mirror of `_http_routes`: one names what the program answers, this
    names what it asks of something else, and a system is only described once
    both halves are present.

    `get("/x")` is ambiguous in Rust -- axum uses it for a method router and
    every client crate uses it for a request -- so extraction is gated on a
    client crate being named in the file. Without that, the same tokens would
    turn every route definition into a phantom outbound call.
    """

    present = {
        token.value
        for token in tokens
        if token.kind == "identifier" and token.value in HTTP_CLIENT_CRATES
    }
    if not present:
        return []
    found: list[tuple[str, str, int]] = []
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value not in HTTP_METHOD_NAMES:
            continue
        if index + 2 >= total or tokens[index + 1].value != "(":
            continue
        argument = tokens[index + 2]
        if argument.kind != "string":
            continue
        target = argument.value
        if not target.startswith(("http://", "https://", "/")):
            continue
        found.append((token.value.upper(), target, token.line))
    return found


def _impl_methods(tokens: list[Token]) -> list[tuple[str, str, int]]:
    """Methods inside `impl` blocks, as `(type, method, line)`.

    A Rust type's behaviour lives in its `impl` blocks, not beside its fields.
    Recording only free functions describes the smaller half of most crates.
    """

    found: list[tuple[str, str, int]] = []
    total = len(tokens)
    index = 0
    while index < total:
        token = tokens[index]
        if token.kind != "identifier" or token.value != "impl":
            index += 1
            continue
        # `impl Trait for Type` and `impl Type` both name the type last.
        header: list[str] = []
        scan = index + 1
        while scan < total and tokens[scan].value != "{":
            if tokens[scan].kind == "identifier":
                header.append("$" if _is_macro_parameter(tokens, scan) else tokens[scan].value)
            scan += 1
        if scan >= total or not header:
            index = scan + 1
            continue
        owner = header[header.index("for") + 1] if "for" in header else header[-1]
        if owner == "$":
            index = scan + 1
            continue
        scan += 1
        depth = 1
        while scan < total and depth:
            current = tokens[scan]
            if current.value == "{":
                depth += 1
            elif current.value == "}":
                depth -= 1
                if depth == 0:
                    break
            elif (
                current.kind == "identifier"
                and current.value == "fn"
                and scan + 1 < total
                and tokens[scan + 1].kind == "identifier"
            ):
                found.append((owner, tokens[scan + 1].value, tokens[scan + 1].line))
            scan += 1
        index = scan + 1
    return found


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
            tokens = tokenize(source)
            file_names = _name_index(tokens)
            file_statics = _mutable_statics(tokens)
            file_impls = _impl_methods(tokens)
            file_errors = _error_surface(tokens)
            file_traits = _trait_implementations(tokens)
            file_routes = _http_routes(tokens)
            file_calls = _client_calls(tokens)
            file_constants = _constants(tokens)
            file_structs = _struct_fields(tokens)
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
                    metadata={
                        "analysis_level": "lexical",
                        **({"name_index": file_names} if file_names else {}),
                        **({"tunables": file_constants} if file_constants else {}),
                        **({"model_fields": file_structs} if file_structs else {}),
                    },
                )
            )

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

            fallible = file_errors["fallible_functions"]
            if fallible:
                first_line = fallible[0][1]
                error_receipt = receipt(
                    file_record.path, first_line, "error_surface", module, excerpt
                )
                named = ", ".join(
                    f"{name} ({count})"
                    for name, count in sorted(
                        file_errors["error_types"].items(), key=lambda pair: (-pair[1], pair[0])
                    )[:5]
                )
                propagated = int(file_errors["propagation_sites"])
                claims.append(
                    self._claim(
                        snapshot,
                        created_at,
                        text=(
                            f"{module} declares {len(fallible)} fallible function(s) returning "
                            f"Result or Option, and propagates with `?` at {propagated} site(s). "
                            + (
                                f"Declared error types: {named}."
                                if named
                                else "No error type is named in those signatures."
                            )
                        ),
                        category="error_surface",
                        supporting=(error_receipt.evidence_id,),
                        importance="medium",
                        path=file_record.path,
                    )
                )

            for method, route_path, route_line in file_routes:
                # A route declared in a test file describes the fixture, not the
                # served surface. Filing both under one category is how a suite
                # of test doubles gets counted as an API.
                mounted = method == "MOUNT"
                category = "http_route"
                if file_record.role == "test":
                    category = "test_route"
                elif mounted:
                    category = "route_mount"
                route_receipt = receipt(
                    file_record.path, route_line, category, f"{method} {route_path}", excerpt
                )
                text = (
                    f"{route_path} mounts a sub-router, so every path it contains is served "
                    f"beneath this prefix."
                    if mounted
                    else f"{method} {route_path} is registered as a route in {module}."
                )
                claims.append(
                    self._claim(
                        snapshot,
                        created_at,
                        text=text,
                        category=category,
                        supporting=(route_receipt.evidence_id,),
                        importance="high" if category == "http_route" else "medium",
                        path=file_record.path,
                    )
                )

            for method, target, call_line in file_calls:
                call_receipt = receipt(
                    file_record.path, call_line, "external_call", f"{method} {target}", excerpt
                )
                claims.append(
                    self._claim(
                        snapshot,
                        created_at,
                        text=(
                            f"{module} issues a {method} request to {target}; that endpoint is "
                            "served by something outside this module."
                        ),
                        category="external_call",
                        supporting=(call_receipt.evidence_id,),
                        importance="high",
                        path=file_record.path,
                    )
                )

            for owner, trait_name, trait_line in file_traits:
                trait_receipt = receipt(
                    file_record.path,
                    trait_line,
                    "trait_implementation",
                    f"{module}::{owner}",
                    excerpt,
                )
                claims.append(
                    self._claim(
                        snapshot,
                        created_at,
                        text=(
                            f"{module}::{owner} implements {trait_name}, so it satisfies that "
                            "contract wherever the trait is accepted."
                        ),
                        category="trait_implementation",
                        supporting=(trait_receipt.evidence_id,),
                        importance="medium",
                        path=file_record.path,
                    )
                )

            for static_name, static_line, reason in file_statics:
                qualified = f"{module}::{static_name}"
                static_receipt = receipt(
                    file_record.path, static_line, "process_local_state", qualified, excerpt
                )
                claims.append(
                    self._claim(
                        snapshot,
                        created_at,
                        text=(
                            f"{qualified} is {reason}; its contents are process-local, so a "
                            "second instance of this program observes none of them."
                        ),
                        category="process_local_state",
                        supporting=(static_receipt.evidence_id,),
                        importance="high",
                        path=file_record.path,
                    )
                )

            for owner, method, method_line in file_impls:
                qualified = f"{module}::{owner}::{method}"
                method_receipt = receipt(
                    file_record.path, method_line, "symbol", qualified, excerpt
                )
                symbols.append(
                    SymbolRecord(
                        symbol_id=stable_id(
                            "symbol",
                            (
                                snapshot.snapshot_id,
                                file_record.path,
                                qualified,
                                method_line,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        path=file_record.path,
                        qualified_name=qualified,
                        kind="method",
                        start_line=method_line,
                        end_line=method_line,
                        language="Rust",
                        analyzer=ANALYZER_VERSION,
                        metadata={"analysis_level": "lexical", "implements_for": owner},
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
                                method_receipt.evidence_id,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        source_symbol_id=module_symbol_id,
                        source_path=file_record.path,
                        relationship="contains",
                        target_ref=qualified,
                        target_symbol_id=None,
                        evidence_id=method_receipt.evidence_id,
                        analyzer=ANALYZER_VERSION,
                    )
                )
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
