# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
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

ANALYZER_NAME = "typescript-lexical"
ANALYZER_VERSION = "typescript-lexical/v1"
ELIGIBLE_LANGUAGES = frozenset({"JavaScript", "JavaScript JSX", "TypeScript", "TypeScript JSX"})
BINDING_KEYWORDS = frozenset({"const", "let", "var"})
CONTAINER_KEYWORDS = frozenset({"class", "interface", "enum", "type"})
# Modifiers sit between a member's position and its name. They are skipped so
# `private static readonly limit = 5` is recorded as `limit`, not `private`.
MEMBER_MODIFIERS = frozenset(
    {
        "public",
        "private",
        "protected",
        "static",
        "readonly",
        "abstract",
        "override",
        "declare",
        "async",
    }
)
# Names that appear in a member position but introduce syntax rather than a
# member. `new` and `import` are call-signature forms; the rest are control flow
# that can legally open a block at container depth in a type body.
NON_MEMBER_NAMES = frozenset(
    {"new", "import", "typeof", "keyof", "infer", "extends", "implements", "in", "of"}
)
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


@dataclass(frozen=True, slots=True)
class Declaration:
    """One name a module introduces, with the kind the tokens actually support."""

    name: str
    kind: str
    start_line: int
    end_line: int


def _initializer_kind(tokens: list[Token], start: int, limit: int = 400) -> str:
    """Classify what a binding is bound to, using only what the tokens show.

    An arrow or the `function` keyword at the initializer's own nesting level
    means the name holds a callable. A literal means a constant. Anything else
    is recorded as a binding rather than guessed at, because `const rows =
    useMemo(() => ..., [])` holds a value even though an arrow appears inside
    it, and depth is what tells those two apart.
    """

    depth = 0
    index = start
    end = min(len(tokens), start + limit)
    # A type annotation and the `=` itself sit between the name and the value.
    while index < end and tokens[index].value != "=":
        if tokens[index].kind == "punctuation" and tokens[index].value in {";", ",", "{"}:
            return "binding"
        index += 1
    index += 1
    first = True
    while index < end:
        token = tokens[index]
        value = token.value
        if token.kind == "punctuation":
            if value in {"(", "[", "{"}:
                depth += 1
            elif value in {")", "]", "}"}:
                depth -= 1
                if depth < 0:
                    break
            elif depth == 0:
                if value == ";":
                    break
                if value == "=" and index + 1 < end and tokens[index + 1].value == ">":
                    return "function"
        elif depth == 0 and token.kind == "identifier" and value == "function":
            return "function"
        elif first and depth == 0 and token.kind in {"string", "number"}:
            return "constant"
        if not (token.kind == "punctuation" and value in {"(", "["}):
            first = False
        index += 1
    return "binding"


def _pattern_bindings(tokens: list[Token], start: int) -> tuple[list[Token], int]:
    """Names introduced by a destructuring pattern, and where the pattern ends.

    In `const { rows, total: count } = props`, `rows` and `count` are bound and
    `total` is a lookup key. An identifier followed by `:` is therefore the key
    side and the name after it is what enters scope.
    """

    opener = tokens[start].value
    closer = {"{": "}", "[": "]"}[opener]
    names: list[Token] = []
    depth = 0
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token.kind == "punctuation":
            if token.value in {"{", "["}:
                depth += 1
            elif token.value in {"}", "]"}:
                depth -= 1
                if depth == 0 and token.value == closer:
                    return names, index
        elif token.kind == "identifier":
            following = tokens[index + 1].value if index + 1 < len(tokens) else ""
            if following == ":":
                index += 2
                continue
            # A default (`= 3`) belongs to the name before it, not after.
            previous = tokens[index - 1].value if index > start else ""
            if previous != "=":
                names.append(token)
        index += 1
    return names, index


def _parameter_names(tokens: list[Token]) -> set[str]:
    """Names bound by a parameter list, including arrow shorthand.

    `mobs.map(m => m.respawn_at)` binds `m` locally. Without this, the receiver
    `m` looks like a global and the reference census fills up with callback
    parameters instead of the platform API it is meant to report.
    """

    names: set[str] = set()

    def collect(open_index: int) -> None:
        depth = 0
        index = open_index
        while index < len(tokens):
            token = tokens[index]
            if token.kind == "punctuation":
                if token.value in {"(", "{", "["}:
                    depth += 1
                elif token.value in {")", "}", "]"}:
                    depth -= 1
                    if depth == 0:
                        return
            elif token.kind == "identifier":
                previous = tokens[index - 1].value if index else ""
                # A name opens the list or follows a separator or a modifier. A
                # type annotation follows `:`, so only the name side counts.
                starts_entry = previous in {"(", ",", "{", "["} or previous in MEMBER_MODIFIERS
                if starts_entry and token.value not in MEMBER_MODIFIERS:
                    names.add(token.value)
            index += 1

    for index, token in enumerate(tokens):
        if token.kind != "punctuation" or token.value != "=":
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].value != ">":
            continue
        previous = tokens[index - 1] if index else None
        if previous is None:
            continue
        if previous.kind == "identifier":
            names.add(previous.value)
            continue
        if previous.value == ")":
            depth = 0
            scan = index - 1
            while scan >= 0:
                value = tokens[scan].value
                if tokens[scan].kind == "punctuation":
                    if value in {")", "}", "]"}:
                        depth += 1
                    elif value in {"(", "{", "["}:
                        depth -= 1
                        if depth == 0:
                            collect(scan)
                            break
                scan -= 1
    # Ordinary and method signatures: an identifier directly followed by `(`
    # that opens a block rather than a call argument list.
    for index, token in enumerate(tokens):
        if (
            token.kind == "identifier"
            and index
            and index + 1 < len(tokens)
            and tokens[index + 1].value == "("
            and tokens[index - 1].value == "function"
        ):
            collect(index + 1)
    return names


def _references(tokens: list[Token], declared: frozenset[str]) -> dict[str, dict[str, Any]]:
    """Platform and library API this module reaches for but does not define.

    `localStorage.setItem`, `document.activeElement`, `new AbortController` and
    `body.getReader` are not this module's symbols, and a symbol index will
    never list them — but they are what a reviewer means by "what does this
    code depend on at runtime". Storage and clipboard access are privacy
    questions, and `eval` is a security one, so the surface is worth naming.

    A name declared in this module is excluded: `engine.run()` where `engine` is
    a local is internal wiring, not an external dependency.
    """

    found: dict[str, dict[str, Any]] = {}

    def record(name: str, line: int, called: bool) -> None:
        entry = found.setdefault(name, {"count": 0, "first_line": line, "called": False})
        entry["count"] = int(entry["count"]) + 1
        entry["first_line"] = min(int(entry["first_line"]), line)
        entry["called"] = bool(entry["called"]) or called

    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier":
            continue
        following = tokens[index + 1].value if index + 1 < total else ""
        previous = tokens[index - 1].value if index else ""
        if previous == ".":
            continue
        if following == "." and index + 2 < total and tokens[index + 2].kind == "identifier":
            if token.value in declared:
                continue
            member = tokens[index + 2]
            called = index + 3 < total and tokens[index + 3].value == "("
            record(f"{token.value}.{member.value}", token.line, called)
            continue
        if previous == "new" and token.value not in declared:
            record(token.value, token.line, True)
    return found


def _declarations(tokens: list[Token]) -> list[Declaration]:
    """Every name a module introduces at a position the tokens make unambiguous.

    Declaration keywords were already covered. What was not: value bindings,
    which is how most modern TypeScript declares both constants and functions,
    and the members of classes, interfaces, enums and object type aliases. A
    module whose functions are all `const handleX = () => {}` previously
    reported no symbols at all.

    Nesting is tracked so only direct members are recorded. An object literal
    built inside a method body sits deeper than the class body and its keys are
    not members of the class.
    """

    result: list[Declaration] = []
    # (container name, the brace depth of its body, whether it is an enum)
    containers: list[tuple[str, int, bool]] = []
    pending: tuple[str, bool] | None = None
    depth = 0
    # Parameter lists and index signatures live in parentheses and brackets. A
    # parameter is not a member of the enclosing class, so member detection is
    # suppressed while either is open.
    parens = 0
    index = 0
    total = len(tokens)

    while index < total:
        token = tokens[index]

        if token.kind == "punctuation":
            if token.value == "{":
                depth += 1
                if pending is not None:
                    containers.append((pending[0], depth, pending[1]))
                    pending = None
            elif token.value == "}":
                while containers and containers[-1][1] == depth:
                    containers.pop()
                depth -= 1
            elif token.value in {"(", "["}:
                parens += 1
            elif token.value in {")", "]"}:
                parens = max(0, parens - 1)
            elif token.value == ";":
                pending = None
            index += 1
            continue

        if token.kind != "identifier":
            index += 1
            continue

        value = token.value
        in_container_body = bool(containers) and depth == containers[-1][1] and parens == 0
        prefix = ".".join(item[0] for item in containers) if in_container_body else ""

        if value in CONTAINER_KEYWORDS or value == "function":
            step = index + 1
            if value == "function" and step < total and tokens[step].value == "*":
                step += 1
            if step < total and tokens[step].kind == "identifier":
                name_token = tokens[step]
                kind = "function" if value == "function" else value
                qualified = f"{prefix}.{name_token.value}" if prefix else name_token.value
                result.append(Declaration(qualified, kind, token.line, name_token.end_line))
                if value in CONTAINER_KEYWORDS:
                    pending = (name_token.value, value == "enum")
                index = step + 1
                continue

        if value in BINDING_KEYWORDS:
            step = index + 1
            if step < total and tokens[step].value in {"{", "["}:
                names, closed = _pattern_bindings(tokens, step)
                for name_token in names:
                    qualified = f"{prefix}.{name_token.value}" if prefix else name_token.value
                    result.append(
                        Declaration(qualified, "binding", name_token.line, name_token.end_line)
                    )
                index = closed + 1
                continue
            if step < total and tokens[step].kind == "identifier":
                name_token = tokens[step]
                kind = _initializer_kind(tokens, step + 1)
                # A binding inside a function body is a local, not part of the
                # module's surface. It is still recorded, because a reader
                # searching for the name needs to find where it lives, but it is
                # not dressed up as a member of whatever class encloses it.
                if depth > 0 and not in_container_body and kind != "function":
                    kind = "local"
                qualified = f"{prefix}.{name_token.value}" if prefix else name_token.value
                result.append(Declaration(qualified, kind, name_token.line, name_token.end_line))
                index = step + 1
                continue

        if in_container_body and value not in NON_MEMBER_NAMES:
            step = index
            while step < total and tokens[step].value in MEMBER_MODIFIERS:
                step += 1
            if step < total and tokens[step].kind == "identifier":
                name_token = tokens[step]
                following = tokens[step + 1].value if step + 1 < total else ""
                is_enum = containers[-1][2]
                member: str | None = None
                if following in {"(", "<"}:
                    member = "method"
                elif following in {":", "?"}:
                    member = "property"
                elif is_enum and following in {",", "=", "}"}:
                    member = "enum_member"
                elif following == "=":
                    member = "property"
                if member is not None:
                    qualified = f"{prefix}.{name_token.value}"
                    result.append(
                        Declaration(qualified, member, name_token.line, name_token.end_line)
                    )
                    index = step + 1
                    continue

        index += 1

    return result


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


MIN_STATE_VALUES = 2


def _state_fields(tokens: list[Token]) -> dict[str, dict[str, Any]]:
    """Value domains observable from a token stream.

    A name assigned or compared against two or more distinct string literals has
    an observable domain. This is lexical: it cannot distinguish a state variable
    from any other string-valued name, and it records no transitions, because the
    enclosing condition is not recoverable from tokens alone.
    """

    values: dict[str, set[str]] = {}
    entries: dict[str, set[tuple[str, str, int]]] = {}

    for index in range(1, len(tokens) - 1):
        operator = tokens[index]
        if operator.kind != "punctuation" or operator.value not in {"=", "!", "<", ">"}:
            continue
        name = tokens[index - 1]
        if name.kind != "identifier":
            continue
        cursor = index + 1
        while (
            cursor < len(tokens)
            and tokens[cursor].kind == "punctuation"
            and tokens[cursor].value in {"=", "!"}
        ):
            cursor += 1
        if cursor >= len(tokens) or tokens[cursor].kind != "string":
            continue
        literal = tokens[cursor].value
        if not literal or len(literal) > 40:
            continue
        values.setdefault(name.value, set()).add(literal)
        if operator.value == "=" and cursor == index + 1:
            entries.setdefault(name.value, set()).add((literal, "", name.line))

    return {
        field: {
            "values": sorted(observed),
            "entries": sorted(entries.get(field, set())),
        }
        for field, observed in values.items()
        if len(observed) >= MIN_STATE_VALUES
    }


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

            file_state_fields = _state_fields(file_tokens)
            file_declarations = _declarations(file_tokens)
            file_references = _references(
                file_tokens,
                frozenset(
                    {item.name.rsplit(".", 1)[-1] for item in file_declarations}
                    | _parameter_names(file_tokens)
                ),
            )
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
                    metadata={
                        "analysis_level": "lexical",
                        **({"state_fields": file_state_fields} if file_state_fields else {}),
                        **({"external_references": file_references} if file_references else {}),
                    },
                )
            )

            fetch_evidence: list[str] = []
            localhost_evidence: list[str] = []
            hook_evidence: dict[str, list[str]] = {name: [] for name in REACT_HOOKS}
            store_evidence: dict[str, list[str]] = {name: [] for name in CLIENT_STORES}
            test_evidence: list[str] = []

            for declaration in file_declarations:
                qualified = f"{module}.{declaration.name}"
                receipt = add_evidence(
                    file_record.path,
                    declaration.start_line,
                    declaration.end_line,
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
                        declaration.kind,
                        declaration.start_line,
                        ANALYZER_VERSION,
                    ),
                )
                symbols.append(
                    SymbolRecord(
                        symbol_id=symbol_id,
                        snapshot_id=snapshot.snapshot_id,
                        path=file_record.path,
                        qualified_name=qualified,
                        kind=declaration.kind,
                        start_line=declaration.start_line,
                        end_line=declaration.end_line,
                        language=file_record.language,
                        analyzer=ANALYZER_VERSION,
                        metadata={
                            "analysis_level": "lexical",
                            **({"state_fields": file_state_fields} if file_state_fields else {}),
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

            for index, token in enumerate(file_tokens):
                following = _next_token(file_tokens, index)
                next_value = file_tokens[following].value if following is not None else None

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
