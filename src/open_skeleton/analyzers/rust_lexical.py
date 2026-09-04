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
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from open_skeleton.analyzers.base import declares_a_number, render_declared_type
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
from open_skeleton.policy import describes_the_product

ANALYZER_NAME = "rust-lexical"
ANALYZER_VERSION = "rust-lexical/v1"
ELIGIBLE_LANGUAGES = frozenset({"Rust"})

# `const fn` and `async fn` declare functions. The qualifier comes first, so
# a reader looking for `const <name>` finds `fn` and records a constant
# called `fn` -- a name no crate contains.
FUNCTION_QUALIFIERS = frozenset({"fn", "async", "unsafe", "extern"})
ITEM_KEYWORDS = {
    "fn": "function",
    "struct": "struct",
    "enum": "enum",
    "trait": "trait",
    "union": "union",
}
# Names that abort the process or discard an error, keyed by how they read at a
# call site. `unwrap_or*` variants are excluded: they supply a fallback.
# Everything below aborts the thread on its failing path, and reporting them as
# one number is useless. On a compiler of 72 Rust files the single figure read
# "4,192 panicking call sites" -- of which 73% were assertions checking
# invariants, which is a rigour signal, and 2 were `todo!`. The one number a
# reader wants (175 bare `unwrap`s) sat behind an aggregate 24 times larger.
# Technically true and actively misleading is the failure this project exists
# to avoid, so the families are counted and reported apart.
PANIC_FAMILIES: dict[str, frozenset[str]] = {
    # Work the author has not written. Rare and worth naming.
    "unfinished": frozenset({"todo", "unimplemented"}),
    # A stated impossibility. Deliberate by construction.
    "explicit": frozenset({"panic", "unreachable"}),
    # An invariant check. `assert_ne` and `debug_assert` were missing here,
    # which undercounted this family by 50 on the compiler above.
    "assertion": frozenset({"assert", "assert_eq", "assert_ne", "debug_assert"}),
}
PANIC_MACROS = frozenset().union(*PANIC_FAMILIES.values())

# Extraction without a proof. `expect` carries the author's reason and `unwrap`
# carries nothing, so a reader auditing them starts with the second.
UNCHECKED_METHODS = frozenset({"unwrap", "unwrap_err"})
DOCUMENTED_METHODS = frozenset({"expect", "expect_err"})
PANIC_METHODS = UNCHECKED_METHODS | DOCUMENTED_METHODS

# Wording and weight per family. Assertions sit at `low` deliberately: they are
# the most numerous and the least actionable, and at `high` they displaced
# every other finding from the summary.
PANIC_REPORTING: dict[str, tuple[str, str]] = {
    "unfinished": (
        (
            "{count} site(s) marked `todo!` or `unimplemented!` appear in Rust source; "
            "each names work not written, and reaching one aborts the thread."
        ),
        "high",
    ),
    "unchecked": (
        (
            "{count} call(s) to `unwrap` or `unwrap_err` appear in Rust source, extracting "
            "a value without recording why it must be there. Whether any can fail is not "
            "decided here."
        ),
        "high",
    ),
    "documented": (
        (
            "{count} call(s) to `expect` or `expect_err` appear in Rust source. Each states "
            "a reason the value must be present; the reason is the author's assertion, not "
            "a proof, and is not checked here."
        ),
        "medium",
    ),
    "explicit": (
        (
            "{count} explicit `panic!` or `unreachable!` site(s) appear in Rust source, "
            "each declaring a state the author treats as impossible."
        ),
        "medium",
    ),
    "assertion": (
        (
            "{count} assertion site(s) (`assert!`, `assert_eq!`, `assert_ne!`, "
            "`debug_assert!`) appear in Rust source. These check invariants rather than "
            "handle failure, and their number reflects how much the code checks itself."
        ),
        "low",
    ),
}
HTTP_METHOD_NAMES = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})
# Calls whose first string argument is a served path. `nest` and `scope` mount a
# sub-router under a prefix, which is a mount point rather than an endpoint.
MOUNT_BUILDERS = frozenset({"nest", "scope", "nest_service"})
ROUTE_BUILDERS = frozenset({"route", "resource"}) | MOUNT_BUILDERS
# `get("/x")` means a request only if one of these is in scope; otherwise it is
# axum's method router and reading it as a call would invent outbound traffic.
HTTP_CLIENT_CRATES = frozenset({"reqwest", "ureq", "surf", "isahc", "hyper"})
# Control flow that takes a parenthesis, and the names Rust programs write
# most often in constructor position. Neither is a call to a definition.
NON_CALL_KEYWORDS = frozenset(
    # `pub(crate)` is a visibility qualifier wearing a call's shape.
    {"if", "while", "match", "for", "return", "in", "let", "fn", "as", "where", "pub", "mut"}
)
# `Some(x)` and `Self(x)` construct a value; they call no definition, and
# counting them fills the call graph with names nothing declares.
ENUM_CONSTRUCTORS = frozenset({"Some", "None", "Ok", "Err", "Self"})
# Words that qualify a type without naming it. `&mut Foo` and `dyn Foo`
# are both implemented for Foo.
TYPE_QUALIFIERS = frozenset({"mut", "dyn", "impl", "const"})
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
        if (
            character == "r"
            and index + 2 < length
            and source[index + 1] == "#"
            and IDENTIFIER_START.match(source[index + 2])
        ):
            # A raw identifier: `r#async` names something `async`, which is how
            # Rust lets a keyword be used as a name. Raw strings were handled
            # and this was not, so `fn r#async(...)` was recorded as a function
            # called `r` -- a name that appears nowhere in the crate. The
            # following character tells the two apart, since `r#"` opens a
            # string and `r#a` opens a name.
            start = index + 2
            cursor = start
            while cursor < length and IDENTIFIER_BODY.match(source[cursor]):
                cursor += 1
            tokens.append(Token("identifier", source[start:cursor], line))
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
            # A lifetime. It was previously skipped without emitting, which
            # left the name after the tick to be read as an ordinary
            # identifier -- so `impl Matcher for &'a Foo` reported the owner
            # as `a`. Emitting it with its own kind means every consumer that
            # filters on `identifier` ignores it for free.
            index += 1
            start = index
            while index < length and IDENTIFIER_BODY.match(source[index]):
                index += 1
            if index > start:
                tokens.append(Token("lifetime", source[start:index], line))
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
    # `_declared_items` and `_trait_implementations` both skip macro bodies and
    # this did not, so a constant inside `rgtest! { ... }` was recorded while a
    # function beside it was not. Whether a macro body is a template or real
    # code cannot be told lexically, so the module applies one rule rather than
    # a different answer per extractor.
    templates = _macro_body_spans(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value not in {"const", "static"}:
            continue
        if any(start < index < end for start, end in templates):
            continue
        step = index + 1
        if step < total and tokens[step].value == "mut":
            step += 1
        if step >= total or tokens[step].kind != "identifier":
            continue
        # `const fn parse(...)` declares a function. The qualifier comes first,
        # so a reader looking for `const <name>` finds `fn` and records a
        # constant called `fn`, which no crate contains.
        if tokens[step].value in FUNCTION_QUALIFIERS:
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
                        "annotation": render_declared_type(parts),
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


def _macro_body_spans(tokens: list[Token]) -> list[tuple[int, int]]:
    """Token ranges inside a macro body, which hold templates rather than code.

    `quote! { impl #generics Args for #ident #where_clause { ... } }` looks
    exactly like an implementation and is not one: it is text a macro will
    emit, for a type whose name is substituted later. Reading it as real
    reported implementations on `where_clause`, a token that names nothing.

    A real Rust parser has this for free -- `syn` treats a macro invocation as
    one opaque item and never descends -- so the lexical reader has to be told
    the same boundary explicitly.
    """

    spans: list[tuple[int, int]] = []
    total = len(tokens)
    index = 0
    while index < total:
        # `quote! {` puts the body straight after the bang. `macro_rules! name {`
        # puts the macro's own name in between, so a detector that expects them
        # adjacent misses every macro definition -- which is where the densest
        # templates live.
        opener = 0
        if (
            tokens[index].kind == "identifier"
            and index + 2 < total
            and tokens[index + 1].value == "!"
        ):
            if tokens[index + 2].value in {"{", "(", "["}:
                # A keyword is never a macro name, and `if !(ready || set)`
                # has the same three-token shape as `vec![...]`. Accepting it
                # read the whole condition as a macro body and dropped the
                # calls inside, which is the negated-condition defect again
                # in its parenthesised form.
                if tokens[index].value in NON_CALL_KEYWORDS:
                    index += 1
                    continue
                opener = index + 2
            elif (
                # Only `macro_rules!` puts a name between the bang and the
                # body. Accepting any identifier there made `if !ready(x)`
                # match the same shape -- identifier, bang, identifier,
                # delimiter -- so the condition was read as a macro body and
                # every call inside it was discarded. `syn` counted 19 calls
                # across 13 files of one crate that this never reported, and
                # capability tracing runs on exactly those edges.
                tokens[index].value == "macro_rules"
                and index + 3 < total
                and tokens[index + 2].kind == "identifier"
                and tokens[index + 3].value in {"{", "(", "["}
            ):
                opener = index + 3
        if opener:
            opening = tokens[opener].value
            closing = {"{": "}", "(": ")", "[": "]"}[opening]
            depth = 1
            scan = opener + 1
            while scan < total and depth:
                if tokens[scan].value == opening:
                    depth += 1
                elif tokens[scan].value == closing:
                    depth -= 1
                scan += 1
            spans.append((index, scan))
            index = scan
            continue
        index += 1
    return spans


def _module_names(paths: Iterable[str]) -> dict[str, str]:
    """Rust path per file, disambiguated where two crate roots share a name.

    A package holding both `src/lib.rs` and `src/main.rs` has two crate roots
    and Cargo names them both after the package, so both files reduced to the
    same Rust path. The document then carried two claims with one subject and
    different numbers -- `cranelift_feasibility declares 1 fallible
    function(s)` directly above `cranelift_feasibility declares 16` -- which
    reads as a contradiction and is really two crates.

    The library keeps the bare name because that is the path other crates
    really use to reach its items. The colliding roots take their file stem
    as a segment: a binary crate's items cannot be named from outside it at
    all, so there is no true external path for the added segment to
    contradict, and it says which file the facts came from.
    """

    claimed: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        claimed[_module_name(path)].append(path)
    resolved: dict[str, str] = {}
    for name, owners in claimed.items():
        if len(owners) == 1:
            resolved[owners[0]] = name
            continue
        for path in owners:
            stem = path.rsplit("/", 1)[-1].removesuffix(".rs")
            resolved[path] = name if stem == "lib" else f"{name}::{stem}"
    return resolved


MAX_ENUM_VARIANTS = 64


def _declared_enums(tokens: list[Token]) -> dict[str, dict[str, Any]]:
    """Enum variants, which are how Rust declares a closed set of values.

    A Python module states a vocabulary with a frozenset and TypeScript with a
    union of string literals; both are read. Rust states one with an enum and
    none of it was read at all, so a command-line parser could declare its
    entire error vocabulary, its argument actions and its colour modes and the
    specification would report none of them. Seven of nine unanswered
    questions on the first Rust fixture were exactly this.

    A variant carrying a payload is still a variant: `Io(std::io::Error)` and
    `Custom { code: i32 }` name members of the set, and the payload is the
    shape of one member rather than another member. Only the names are taken.

    Two variants are required, for the same reason one literal is a constant
    rather than a vocabulary.
    """

    templates = _macro_body_spans(tokens)
    found: dict[str, dict[str, Any]] = {}
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "enum":
            continue
        if index + 2 >= total or any(start < index < end for start, end in templates):
            continue
        name = tokens[index + 1]
        if name.kind != "identifier":
            continue
        # Step over any generic parameter list before the body.
        cursor = index + 2
        if tokens[cursor].value == "<":
            depth = 0
            while cursor < total:
                if tokens[cursor].value == "<":
                    depth += 1
                elif tokens[cursor].value == ">":
                    depth -= 1
                    if depth == 0:
                        cursor += 1
                        break
                cursor += 1
        if cursor >= total or tokens[cursor].value != "{":
            continue

        variants: list[str] = []
        depth = 0
        expecting = True
        while cursor < total:
            current = tokens[cursor]
            if current.kind == "punctuation" and current.value in {"{", "(", "["}:
                depth += 1
                cursor += 1
                continue
            if current.kind == "punctuation" and current.value in {"}", ")", "]"}:
                depth -= 1
                cursor += 1
                if depth == 0:
                    break
                continue
            # Only at the enum body's own depth. A field inside a struct-like
            # variant is part of that variant, not a member beside it.
            if depth == 1:
                if current.kind == "punctuation" and current.value == ",":
                    expecting = True
                elif current.kind == "identifier" and expecting and current.value != "pub":
                    variants.append(current.value)
                    expecting = False
                elif current.kind == "punctuation" and current.value == "#":
                    # An attribute such as `#[non_exhaustive]` sits where a
                    # variant would; skipping its brackets is handled by depth.
                    expecting = True
            cursor += 1
        if 2 <= len(variants) <= MAX_ENUM_VARIANTS:
            found[name.value] = {"members": variants, "line": name.line}
    return found


def _declared_items(tokens: list[Token]) -> list[tuple[str, str, int]]:
    """`(kind, name, line)` for the items a file actually declares.

    `macro_rules! make { ($n:ident) => { pub struct Generated; }; }` declares
    no struct. It describes one a caller may ask for, under a name that is
    substituted at expansion. Recording it put a type in the symbol index that
    nothing in the crate defines, and a reader cannot tell that entry from a
    real one.

    The same exclusion `_trait_implementations` uses, applied to the other
    half of what this analyzer says a file contains.
    """

    templates = _macro_body_spans(tokens)
    found: list[tuple[str, str, int]] = []
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value not in ITEM_KEYWORDS:
            continue
        if index + 1 >= total or any(start < index < end for start, end in templates):
            continue
        name = tokens[index + 1]
        if name.kind == "identifier" and not _is_macro_parameter(tokens, index + 1):
            found.append((ITEM_KEYWORDS[token.value], name.value, token.line))
    return found


def _trait_implementations(tokens: list[Token]) -> list[tuple[str, str, int]]:
    """`impl Trait for Type` pairs: the contracts a type actually satisfies.

    A trait implementation is where a Rust type declares it can be used a
    certain way, which is the closest thing the language has to the capability
    a route exposes in a service. An inherent `impl Type` block declares no
    contract and is excluded.
    """

    found: list[tuple[str, str, int]] = []
    total = len(tokens)
    templates = _macro_body_spans(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "impl":
            continue
        if any(start < index < end for start, end in templates):
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
            # A `where` clause names bounds, not the type being implemented
            # for. Reading past it made `impl Trait for Foo where T: Send`
            # report an implementation on `Send`.
            if tokens[scan].kind == "identifier" and value == "where" and generics <= 0:
                break
            if value == "<":
                generics += 1
            elif value == ">":
                generics -= 1
            elif tokens[scan].kind == "identifier" and generics <= 0:
                if _is_macro_parameter(tokens, scan):
                    header.append("$")
                elif value not in TYPE_QUALIFIERS:
                    header.append(value)
            scan += 1
        if "for" not in header:
            continue
        divider = header.index("for")
        trait_name = header[divider - 1] if divider else ""
        # `impl From<E> for std::io::Error` implements it for Error, not for
        # std. The segments after `for` are one path, so the owner is its last
        # segment rather than its first.
        owner = header[-1] if divider + 1 < len(header) else ""
        if trait_name and owner and "$" not in {trait_name, owner}:
            found.append((owner, trait_name, token.line))
    return found


def _environment_reads(tokens: list[Token]) -> list[tuple[str, str, int]]:
    """Environment settings a crate reads, as `(name, when, line)`.

    The Python analyzer has reported `os.getenv` since the beginning and the
    Rust one reported nothing, so a crate that will not start without
    `DATABASE_URL` said so in Python and stayed silent in Rust. What a program
    needs from its environment is the same question in both.

    `when` separates two things Rust spells almost identically. `env::var` is
    read when the program runs and can be missing on the machine that runs it;
    `env!` is substituted by the compiler, so its value is fixed in the binary
    and a reader looking for something to configure will never find it. Naming
    them the same way would tell an operator to set a variable that nothing
    will ever read.

    A name built at runtime yields no string token and is not recorded: the
    value is not knowable without running the program.
    """

    found: list[tuple[str, str, int]] = []
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier":
            continue
        # `env!("X")` and `option_env!("X")` -- compile time.
        if (
            token.value in {"env", "option_env"}
            and index + 3 < total
            and tokens[index + 1].value == "!"
            and tokens[index + 2].value == "("
            and tokens[index + 3].kind == "string"
        ):
            name = tokens[index + 3].value
            if name:
                found.append((name, "compile time", token.line))
            continue
        # `env::var("X")`, with or without a `std::` prefix -- run time.
        if token.value == "var" and index + 2 < total:
            preceded_by_env = (
                index >= 2
                and tokens[index - 1].value == ":"
                and tokens[index - 2].value == ":"
                and index >= 3
                and tokens[index - 3].value == "env"
            )
            if (
                preceded_by_env
                and tokens[index + 1].value == "("
                and tokens[index + 2].kind == "string"
            ):
                name = tokens[index + 2].value
                if name:
                    found.append((name, "run time", token.line))
    return found


# Items a `pub` can introduce. Wider than `ITEM_KEYWORDS`, which exists to
# create symbols: a `pub const` is part of what a crate exposes even though a
# constant is not an item this analyzer indexes.
PUBLIC_ITEMS = frozenset(
    {"fn", "struct", "enum", "trait", "union", "mod", "const", "type", "static"}
)


def _public_surface(tokens: list[Token]) -> list[tuple[str, str, int]]:
    """Names a crate exposes, as `(kind, name, line)`.

    The Python analyzer reports a module's public surface and the Rust one
    reported nothing, so the primary fact about a library crate -- what a
    caller may depend on -- was missing for every crate this engine read.

    `pub(crate)` is deliberately excluded. It is a visibility qualifier that
    restricts a name to this crate, so counting it as the public surface would
    tell a reader they can depend on something no other crate can reach. Only
    a bare `pub` widens the surface; `pub(super)` and `pub(in path)` are
    narrower still and excluded for the same reason.
    """

    found: list[tuple[str, str, int]] = []
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "pub":
            continue
        cursor = index + 1
        # `pub(crate)`, `pub(super)`, `pub(in ...)` all restrict rather than
        # expose, and every one of them opens with a parenthesis.
        if cursor < total and tokens[cursor].value == "(":
            continue
        # `pub async fn`, `pub unsafe fn`, `pub extern "C" fn`.
        while cursor < total and tokens[cursor].value in FUNCTION_QUALIFIERS - {"fn"}:
            cursor += 1
            if cursor < total and tokens[cursor].kind == "string":
                cursor += 1
        if cursor >= total or tokens[cursor].value not in PUBLIC_ITEMS:
            continue
        kind = tokens[cursor].value
        name_index = cursor + 1
        if name_index >= total or tokens[name_index].kind != "identifier":
            continue
        found.append((kind, tokens[name_index].value, token.line))
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


def _attribute_spans(tokens: list[Token]) -> list[tuple[int, int]]:
    """Token ranges inside `#[...]`, which configure code rather than run it.

    `#[derive(Debug)]` and `#[cfg(not(any(unix, windows)))]` are identifiers
    followed by parentheses, which is exactly the shape of a call. Reading them
    as calls filled the graph with `derive`, `cfg`, `not`, `any` and `deny` --
    names that resolve to no definition anywhere in the crate, in more than a
    hundred ripgrep files.
    """

    spans: list[tuple[int, int]] = []
    total = len(tokens)
    index = 0
    while index < total:
        if tokens[index].value == "#":
            opener = index + 1
            # `#![...]` is an inner attribute; the bang sits before the bracket.
            if opener < total and tokens[opener].value == "!":
                opener += 1
            if opener < total and tokens[opener].value == "[":
                depth = 1
                scan = opener + 1
                while scan < total and depth:
                    if tokens[scan].value == "[":
                        depth += 1
                    elif tokens[scan].value == "]":
                        depth -= 1
                    scan += 1
                spans.append((index, scan))
                index = scan
                continue
        index += 1
    return spans


def _declared_clap_flags(tokens: list[Token]) -> dict[str, int]:
    """Long options a clap derive declares, with the line that declares each.

    A command line is the whole interface of a tool, and this reader knew
    Python's and not Rust's, so `command_line_interface` fired for exactly one
    repository -- which is the shape of an analyzer written against one
    codebase rather than a property of the world.

    Two forms appear. `#[arg(long = "name")]` states the flag, and it is
    quoted as written. Bare `#[arg(long)]` does not: clap derives the flag
    from the field beneath it, lowercasing and replacing `_` with `-`, so
    `github_repo` becomes `--github-repo`.

    That derivation is only safe while the default naming holds. `rename_all`
    changes it for the whole container, and a flag printed under the wrong
    rule is one nobody can type -- worse than an omission, because somebody
    will type what the document says. Where the file mentions `rename_all` at
    all, bare `long` is left unread and only explicit names are reported.
    """

    renamed = any(token.value == "rename_all" for token in tokens)
    spans = _attribute_spans(tokens)
    found: dict[str, int] = {}

    for start, end in spans:
        span = tokens[start:end]
        if not ({"arg", "clap"} & {token.value for token in span}):
            continue
        for offset, token in enumerate(span):
            if token.value != "long":
                continue
            line = token.line
            following = span[offset + 1 : offset + 3]
            if following and following[0].value == "=":
                literal = following[1] if len(following) > 1 else None
                if literal is not None and literal.kind == "string":
                    name = literal.value.strip('"').strip()
                    if name:
                        flag = f"--{name}"
                        found[flag] = min(found.get(flag, line), line)
                continue
            if renamed:
                # The container renames its fields; deriving would be a guess.
                continue
            field = _field_after(tokens, end, spans)
            if field:
                flag = f"--{field.replace('_', '-').lower()}"
                found[flag] = min(found.get(flag, line), line)
    return found


def _field_after(tokens: list[Token], end: int, spans: list[tuple[int, int]]) -> str | None:
    """The struct field name an attribute sits above, if the next item is one.

    Attributes stack, so the search steps over any further `#[...]` before
    looking. It stops at the first item that is not a field: an attribute
    above a function or a type is not describing one, and reading it anyway is
    how a reader starts naming things that do not exist.
    """

    by_start = {span[0]: span[1] for span in spans}
    index = end
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if token.value == "#":
            following = by_start.get(index)
            if following is None:
                return None
            index = following
            continue
        if token.value == "pub":
            index += 1
            continue
        if token.kind == "identifier":
            nxt = index + 1
            if nxt < total and tokens[nxt].value == ":":
                return str(token.value)
            return None
        return None
    return None


def _call_sites(tokens: list[Token]) -> list[tuple[str, int]]:
    """Names invoked as calls, as `(callee, line)`.

    Until this existed the Rust analyzer emitted no `calls` edges at all, so
    every consumer that walks the call graph -- capability tracing above all --
    silently returned nothing for Rust. A 52-module crate with 178 passing
    tests reported no verifying reference for any capability, and the reason
    was not the tracing rules but that there was no graph to trace.

    Lexical resolution means a name, not a target. `parse(x)` records `parse`
    without deciding which `parse` it is, which is the same guarantee the rest
    of this analyzer makes. Three things that look like calls are excluded
    because none of them is one: a declaration's own name after `fn`, control
    flow that takes a parenthesis, and a type in constructor position such as
    `Some(x)` or `Ok(x)`. Macros need no exclusion -- `println!(...)` puts a
    bang between the name and the parenthesis, so requiring them adjacent
    already skips it.
    """

    found: list[tuple[str, int]] = []
    total = len(tokens)
    attributes = _attribute_spans(tokens)
    templates = _macro_body_spans(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or index + 1 >= total:
            continue
        # `value.parse::<u64>()` is a call with a turbofish between the name and
        # the parenthesis. Requiring them adjacent missed every generic call.
        opener = index + 1
        if tokens[opener].value == ":" and opener + 2 < total and tokens[opener + 1].value == ":":
            scan = opener + 2
            if scan < total and tokens[scan].value == "<":
                depth = 1
                scan += 1
                while scan < total and depth:
                    if tokens[scan].value == "<":
                        depth += 1
                    elif tokens[scan].value == ">":
                        depth -= 1
                    scan += 1
                opener = scan
        if opener >= total or tokens[opener].value != "(":
            continue
        if any(start < index < end for start, end in attributes):
            continue
        if any(start < index < end for start, end in templates):
            continue
        name = token.value
        if name in NON_CALL_KEYWORDS or name in ENUM_CONSTRUCTORS:
            continue
        # `Mode::Search(x)` constructs an enum variant and `impl Fn(A)` names
        # a trait bound; neither calls a definition. They cannot be listed
        # like `Some` and `Ok` because they are the crate's own types, but
        # Rust's naming convention separates them: variants and types are
        # capitalised, functions are not, and rustc lints anything else.
        if name[:1].isupper():
            continue
        if index and tokens[index - 1].value == "fn":
            continue
        if _is_macro_parameter(tokens, index):
            continue
        found.append((name, token.line))
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
        # `impl Trait for ()` names its target entirely in punctuation, so the
        # header ends at `for` and there is no identifier after it. Indexing
        # past the end raised `IndexError` and abandoned the whole repository
        # over one statement -- `clap` implements three traits for the unit
        # type, which no fixture written by hand would have thought to include.
        divider = header.index("for") if "for" in header else -1
        if divider >= 0 and divider + 1 >= len(header):
            index = scan + 1
            continue
        owner = header[divider + 1] if divider >= 0 else header[-1]
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
    """The Rust path for a file, which is not the same as its directory path.

    Cargo does not put `src` in a module path and a crate directory named
    `warmboot-core` is `warmboot_core` to the language, so joining directories
    verbatim produced `crates::warmboot-core::src::catalog::layout` -- a name
    that appears in no `use` statement anywhere and that a reader cannot paste
    into one. The file is still named exactly by its receipt; only the module
    path is the language's rather than the filesystem's.
    """

    parts = path.removesuffix(".rs").split("/")
    if parts[-1] in {"mod", "lib", "main"} and len(parts) > 1:
        parts = parts[:-1]
    # A workspace lays crates out as `crates/<name>/src/...`; a single crate as
    # `src/...`. Both put the module root immediately after `src`.
    for root in ("src", "tests", "benches", "examples"):
        if root not in parts:
            continue
        index = parts.index(root)
        crate = parts[index - 1] if index > 0 else ""
        tail = parts[index + 1 :]
        # `tests/`, `benches/` and `examples/` each compile as their own crate
        # rather than as a module of the one beside them, so the file names it
        # and the surrounding directories are Cargo's layout, not a path.
        parts = ([crate] if crate and root == "src" else []) + tail
        break
    return "::".join(item.replace("-", "_") for item in parts if item) or "crate"


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
    eligibility = "language"

    def analyze(self, snapshot: Snapshot) -> AnalysisResult:
        started = time.perf_counter()
        created_at = utc_now()
        symbols: list[SymbolRecord] = []
        edges: list[EdgeRecord] = []
        evidence: list[EvidenceRecord] = []
        claims: list[ClaimRecord] = []
        failures: list[str] = []

        eligible = [item for item in snapshot.files if item.language in ELIGIBLE_LANGUAGES]
        module_names = _module_names(item.path for item in eligible)
        analyzed_files = 0
        unsafe_receipts: list[str] = []
        unsafe_files: set[str] = set()
        panic_receipts: dict[str, list[str]] = defaultdict(list)
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
            module = module_names[file_record.path]
            tokens = tokenize(source)
            file_names = _name_index(tokens)
            file_flags = _declared_clap_flags(tokens)
            # `--github-repo` is not a Rust identifier, so the name walk skips
            # it and a reader searching for a flag finds nothing.
            for flag, flag_line in file_flags.items():
                file_names[flag] = min(file_names.get(flag, flag_line), flag_line)
            file_statics = _mutable_statics(tokens)
            file_impls = _impl_methods(tokens)
            file_errors = _error_surface(tokens)
            file_traits = _trait_implementations(tokens)
            file_routes = _http_routes(tokens)
            file_environment = _environment_reads(tokens)
            file_public = _public_surface(tokens)
            file_calls = _client_calls(tokens)
            file_call_sites = _call_sites(tokens)
            declared_here = {(line, name) for _, name, line in _declared_items(tokens)}
            file_constants = _constants(tokens)
            file_enums = _declared_enums(tokens)
            # Numbers to the tunable index, strings to the value panel.
            # Both were arriving in the first, which put `SERVICE_NAME`
            # in a table titled for numbers a maintainer would tune.
            # A constant whose value this reader never saw stays in the
            # tunable index, which renders a missing value as a dash. The
            # string panel subscripts the value directly, and a Rust `static`
            # declared in one place and assigned in another has none -- which
            # is the case that panel's own comment warned about and this split
            # walked straight into.
            file_strings = {
                name: entry
                for name, entry in file_constants.items()
                if "value" in entry and not declares_a_number(entry["value"])
            }
            file_constants = {
                name: entry for name, entry in file_constants.items() if name not in file_strings
            }
            file_structs = _struct_fields(tokens)
            if file_flags and describes_the_product(file_record.role):
                first_flag_line = min(file_flags.values())
                flag_excerpt = (
                    lines[first_flag_line - 1] if 0 < first_flag_line <= len(lines) else ""
                )
                named = ", ".join(f"`{flag}`" for flag in sorted(file_flags)[:12])
                more = f" and {len(file_flags) - 12:,} more" if len(file_flags) > 12 else ""
                claims.append(
                    self._claim(
                        snapshot,
                        created_at,
                        text=(
                            f"{file_record.path} declares a command-line interface -- "
                            f"{len(file_flags):,} option(s): {named}{more}. These are the "
                            "words a user types; a `fn main` says only that the crate can "
                            "be started."
                        ),
                        category="command_line_interface",
                        supporting=(
                            receipt(
                                file_record.path,
                                first_flag_line,
                                "command_line_interface",
                                module,
                                flag_excerpt,
                            ).evidence_id,
                        ),
                        path=file_record.path,
                    )
                )
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
                        **({"collection_constants": file_enums} if file_enums else {}),
                        **({"string_constants": file_strings} if file_strings else {}),
                    },
                )
            )

            # A `pub` item in a test file is public to the suite, not to a
            # consumer of the crate, and reporting it as the crate's surface
            # says a caller can depend on something no caller can reach. The
            # `fn main` claim above already applies this rule; this one did
            # not, and the engine's own audit flagged the result.
            if file_public and describes_the_product(file_record.role):
                names = sorted({name for _, name, _ in file_public})
                shown = ", ".join(names[:12])
                remainder = len(names) - min(len(names), 12)
                surface = (
                    f"{module} declares {len(names):,} name(s) as its public surface: "
                    f"{shown}{f', and {remainder:,} more' if remainder else ''}. "
                    "`pub(crate)` items are excluded: they are visible inside this crate "
                    "and to nobody depending on it."
                )
                first = file_public[0][2]
                excerpt = lines[first - 1] if 0 < first <= len(lines) else ""
                claims.append(
                    self._claim(
                        snapshot,
                        created_at,
                        text=surface,
                        category="public_api",
                        supporting=(
                            receipt(
                                file_record.path, first, "public_surface", module, excerpt
                            ).evidence_id,
                        ),
                        importance="high",
                        path=file_record.path,
                    )
                )

            for setting, when, line in dict.fromkeys(file_environment):
                excerpt = lines[line - 1] if 0 < line <= len(lines) else ""
                claims.append(
                    self._claim(
                        snapshot,
                        created_at,
                        text=(
                            f"{file_record.path} reads environment setting {setting} at {when}."
                            if when == "run time"
                            else (
                                f"{file_record.path} substitutes environment setting "
                                f"{setting} at {when}, so its value is fixed in the built "
                                "binary rather than read on the machine that runs it."
                            )
                        ),
                        category="configuration_read",
                        supporting=(
                            receipt(
                                file_record.path, line, "environment_read", module, excerpt
                            ).evidence_id,
                        ),
                        importance="medium",
                        path=file_record.path,
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
                    # `_declared_items` is the single decision about what this
                    # file declares, so a template inside a macro body is
                    # excluded here by the same rule rather than a second one
                    # that can drift away from it.
                    if (token.line, name_token.value) in declared_here:
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
                        if (
                            name_token.value == "main"
                            and token.value == "fn"
                            and describes_the_product(file_record.role)
                        ):
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
                        family = "unchecked" if token.value in UNCHECKED_METHODS else "documented"
                        panic_receipts[family].append(
                            receipt(
                                file_record.path, token.line, "panic_site", module, excerpt
                            ).evidence_id
                        )
                    index += 1
                    continue

                if token.value in PANIC_MACROS and index + 1 < len(tokens):
                    following = tokens[index + 1]
                    if following.kind == "punctuation" and following.value == "!":
                        family = next(
                            name
                            for name, members in PANIC_FAMILIES.items()
                            if token.value in members
                        )
                        panic_receipts[family].append(
                            receipt(
                                file_record.path, token.line, "panic_site", module, excerpt
                            ).evidence_id
                        )
                    index += 1
                    continue

                index += 1

            fallible = file_errors["fallible_functions"]
            # An integration test under `tests/` declares its own fallible
            # helpers, and reporting them as the crate's error surface
            # describes the suite: `crates/warmboot-core/tests/compat.rs` was
            # once the whole of what warmboot appeared to say about how it
            # handles failure.
            #
            # That was first fixed here by naming one role and dropping the
            # claim, which was half a rule twice over. Half, because a
            # benchmark is not a test and a reference implementation's error
            # surface went on being reported as the crate's -- nine claims of
            # it, measured by relocating a real crate under `benchmarks/`.
            # Half again, because dropping the claim loses a true fact: a
            # suite's own error handling is worth knowing, filed as the
            # suite's. `analyze_snapshot` re-files by the role of the
            # evidence, so the claim is made here and named correctly there.
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

            for callee, call_line in file_call_sites:
                edges.append(
                    EdgeRecord(
                        edge_id=stable_id(
                            "edge",
                            (
                                snapshot.snapshot_id,
                                module_symbol_id,
                                "calls",
                                callee,
                                call_line,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        source_symbol_id=module_symbol_id,
                        source_path=file_record.path,
                        relationship="calls",
                        target_ref=callee,
                        target_symbol_id=None,
                        evidence_id=receipt(
                            file_record.path, call_line, "call_site", callee, excerpt
                        ).evidence_id,
                        analyzer=ANALYZER_VERSION,
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
                if not describes_the_product(file_record.role):
                    continue
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

        # One claim per family, in the order a reader acts on them. Reported
        # together they were a single figure nobody could use.
        for family, (template, importance) in PANIC_REPORTING.items():
            found = panic_receipts.get(family, [])
            if not found:
                continue
            claims.append(
                self._claim(
                    snapshot,
                    created_at,
                    text=template.format(count=f"{len(found):,}"),
                    category="panic_site",
                    supporting=tuple(sorted(set(found))[:200]),
                    importance=importance,
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
