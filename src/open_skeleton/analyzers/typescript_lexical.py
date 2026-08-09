# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict
from collections.abc import Iterable
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
# Control flow that is followed by a parenthesis and is not a call.
JS_KEYWORDS = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "typeof",
        "await",
        "yield",
        "delete",
        "void",
        "in",
        "of",
        "do",
        "else",
        "function",
        "import",
        "export",
        "const",
        "let",
        "var",
        "class",
        "new",
        "throw",
        "case",
    }
)
REACT_HOOKS = frozenset(
    {"useCallback", "useContext", "useEffect", "useMemo", "useReducer", "useRef", "useState"}
)
# Keywords that may sit between `export` and the name being exported.
EXPORTABLE_KEYWORDS = frozenset(
    {
        "function",
        "const",
        "let",
        "var",
        "class",
        "interface",
        "type",
        "enum",
        "async",
        "default",
        "abstract",
        "declare",
        "namespace",
    }
)
# Keywords after which a `/` opens a regex. Every other identifier is a
# value, and a value can be divided.
REGEX_PRECEDING_KEYWORDS = frozenset(
    {
        "return",
        "typeof",
        "instanceof",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "throw",
        "case",
        "do",
        "else",
        "yield",
        "await",
    }
)
CLIENT_STORES = frozenset({"localStorage", "sessionStorage"})
# Calls that return something requests are registered on. A name is treated as
# a server only by being bound to one of these, never by being called `app`.
SERVER_FACTORIES = frozenset(
    {"express", "Router", "fastify", "Hono", "Koa", "polka", "restify", "connect"}
)
# Modules whose default export issues requests. A local name counts as a
# client only by being imported from one of these.
CLIENT_MODULES = frozenset({"axios", "ky", "got", "superagent", "node-fetch", "undici"})
SERVER_METHOD_NAMES = frozenset(
    {"get", "post", "put", "delete", "patch", "head", "options", "all", "use"}
)
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})  # noqa: S104
ORIGIN_LITERAL = re.compile(
    r"^(?P<scheme>https?|wss?)://(?P<host>[A-Za-z0-9.\-]+)(?::\d+)?(?:[/?#]|$)"
)


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    line: int
    end_line: int


def _regex_may_start(emitted: list[Token]) -> bool:
    """Whether a `/` here opens a regex rather than dividing.

    JavaScript cannot be tokenized without this decision, and it cannot be made
    from the slash alone: `a / b` divides and `/ab/.test(x)` matches. What
    settles it is what came before. A value can be divided, so an identifier, a
    number, a string, or a closing bracket means division. Anything else --
    an operator, a comma, an opening bracket, a keyword, the start of the file
    -- cannot be divided and therefore opens a pattern.

    `}` is read as allowing a regex. It usually closes a block, and a statement
    may begin with a regex; reading it as division would swallow one.
    """

    if not emitted:
        return True
    previous = emitted[-1]
    if previous.kind in {"number", "string"}:
        return False
    if previous.kind == "identifier":
        return previous.value in REGEX_PRECEDING_KEYWORDS
    return previous.value not in {")", "]"}


def _read_regex(source: str, index: int) -> int:
    """Return the index just past a regex literal beginning at ``index``.

    Returns ``index`` when the slash does not in fact open one, so the caller
    can fall through rather than consume the rest of the file on a guess.
    """

    cursor = index + 1
    length = len(source)
    in_class = False
    while cursor < length:
        character = source[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == "\n":
            # A regex literal cannot span lines. This was division, or broken
            # source; either way the slash is not a pattern opener.
            return index
        if character == "[":
            in_class = True
        elif character == "]":
            in_class = False
        elif character == "/" and not in_class:
            cursor += 1
            while cursor < length and source[cursor].isalpha():
                cursor += 1
            return cursor
        cursor += 1
    return index


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
        if character == "/" and _regex_may_start(result):
            end = _read_regex(source, index)
            if end > index:
                # The body is deliberately not kept. A regex is a pattern, not
                # a name or a value this engine reports, and the only thing
                # that matters is consuming it so its contents stop being read
                # as code. `/^[^\s@"]+$/` contains a quote, and treating that
                # quote as a string opener swallowed the rest of the file --
                # every declaration after the first such regex disappeared.
                line += source[index:end].count("\n")
                index = end
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


def _module_names(paths: Iterable[str]) -> dict[str, str]:
    """Module name per file, disambiguated where two files share one.

    The name drops the extension, so `src/util.ts` and `src/util.js` both
    reduce to `src.util` -- a compiled artifact sitting beside its source, an
    `index.ts` beside an `index.tsx`, a `.mjs` beside a `.ts`. All are
    ordinary, and all produced two unrelated files under one subject: the
    document then carries two `src.util exports ...` rows with different
    names in them and reads as a contradiction.

    Rust hit this an hour earlier with a package holding both crate roots,
    and Python before that with two distributions in one workspace. Only the
    colliding names take their extension back, because for every other file
    the extension carries no information a reader needs.
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
            suffix = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
            resolved[path] = f"{name}.{suffix}" if suffix else name
    return resolved


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


MUTABLE_CONSTRUCTORS = frozenset({"Map", "Set", "WeakMap", "WeakSet", "Array"})
MUTATING_METHODS = frozenset(
    {"set", "add", "delete", "push", "pop", "shift", "unshift", "splice", "clear", "sort"}
)


def _module_state(tokens: list[Token], declarations: list[Declaration]) -> list[tuple[str, int]]:
    """Module-scope containers that something writes to while the process runs.

    This is the same property the Python analyzer reports as process-local
    state and the Rust one reports for a shared static: a container declared
    once at module scope and mutated at runtime holds values that live in one
    process and are invisible to a second. Naming it the same way in every
    language is the point — a reader should not have to learn three vocabularies
    for one fact.

    A container that is never mutated is a lookup table, and calling it state
    would repeat an error this codebase has already made twice.
    """

    module_level = {
        item.name: item.start_line
        for item in declarations
        if "." not in item.name and item.kind in {"binding", "constant"}
    }
    if not module_level:
        return []

    # A declaration only counts as a container if it is built like one.
    containers: dict[str, int] = {}
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value not in module_level:
            continue
        following = tokens[index + 1].value if index + 1 < total else ""
        if following != "=":
            continue
        scan = index + 2
        while scan < total and tokens[scan].value in {"new", "await"}:
            scan += 1
        if scan >= total:
            continue
        candidate = tokens[scan]
        if candidate.value in {"{", "["} or (
            candidate.kind == "identifier" and candidate.value in MUTABLE_CONSTRUCTORS
        ):
            containers[token.value] = module_level[token.value]

    mutated: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value not in containers:
            continue
        following = tokens[index + 1].value if index + 1 < total else ""
        after = tokens[index + 2] if index + 2 < total else None
        if following == "." and after is not None and after.value in MUTATING_METHODS:
            mutated.setdefault(token.value, containers[token.value])
        elif following == "[":
            # `cache[key] = value` is a write; `cache[key]` alone is a read.
            scan = index + 2
            depth = 1
            while scan < total and depth:
                if tokens[scan].value == "[":
                    depth += 1
                elif tokens[scan].value == "]":
                    depth -= 1
                scan += 1
            if scan < total and tokens[scan].value == "=":
                mutated.setdefault(token.value, containers[token.value])
    return sorted(mutated.items(), key=lambda pair: pair[1])


def _environment_reads(tokens: list[Token]) -> dict[str, int]:
    """Names read from `process.env`, where configuration enters a Node process."""

    found: dict[str, int] = {}
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "env":
            continue
        if index == 0 or tokens[index - 1].value != ".":
            continue
        if index < 2 or tokens[index - 2].value != "process":
            continue
        following = tokens[index + 1].value if index + 1 < total else ""
        if index + 2 >= total:
            continue
        # `process.env.NAME` and `process.env["NAME"]` are the same read.
        subscripted = following == "[" and tokens[index + 2].kind == "string"
        attributed = following == "." and tokens[index + 2].kind == "identifier"
        if subscripted or attributed:
            found.setdefault(tokens[index + 2].value, token.line)
    return found


def _throw_sites(tokens: list[Token]) -> dict[str, int]:
    """Exception types this module throws, with the line each first appears.

    The Python analyzer records what a handler raises and Rust records what a
    signature can return. This is the same question for a language that throws:
    what can come out of a call that is not a value.
    """

    found: dict[str, int] = {}
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "throw":
            continue
        scan = index + 1
        if scan < total and tokens[scan].value == "new":
            scan += 1
        if scan < total and tokens[scan].kind == "identifier":
            found.setdefault(tokens[scan].value, token.line)
    return found


def _name_index(tokens: list[Token]) -> dict[str, int]:
    """Every identifier this module mentions, with the line it first appears.

    A concordance rather than analysis, for the same reason as its Python
    counterpart: the structured panels judge a name by its position, and this
    answers only whether the name occurs here at all. String literals that are
    valid identifiers are included, since a JSX prop and a DOM event name both
    reach the code as strings.
    """

    found: dict[str, int] = {}
    for token in tokens:
        if (token.kind == "identifier" and token.value not in JS_KEYWORDS) or (
            token.kind == "string" and token.value.isidentifier() and len(token.value) > 2
        ):
            value = token.value
        else:
            continue
        found[value] = min(found.get(value, token.line), token.line)
    return found


def _object_keys(tokens: list[Token], declared: frozenset[str]) -> dict[str, dict[str, Any]]:
    """Field names written as object literal keys.

    A request body assembled inline — `{ player_name, target_hp, rested_bonus }`
    — is a contract with the server that exists only as these keys. Nothing
    else records it: there is no model class to read, and the symbol index
    holds the function that builds the object rather than its shape.

    A key is counted when it sits in key position, which means followed by a
    colon inside braces, or standing alone as shorthand before a comma or the
    closing brace. Anything computed is skipped rather than guessed at.
    """

    found: dict[str, dict[str, Any]] = {}
    depth = 0
    brace_stack: list[bool] = []
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind == "punctuation":
            if token.value == "{":
                # An object literal opens where a value is expected. After `)`
                # or an identifier it is a block, and blocks hold statements.
                previous = tokens[index - 1].value if index else "{"
                brace_stack.append(previous in {"=", "(", ",", ":", "[", "return", "=>", "{"})
                depth += 1
            elif token.value == "}":
                if brace_stack:
                    brace_stack.pop()
                depth -= 1
            continue
        if token.kind != "identifier" or not brace_stack or not brace_stack[-1]:
            continue
        previous = tokens[index - 1].value if index else ""
        following = tokens[index + 1].value if index + 1 < total else ""
        if previous == ".":
            continue
        is_pair = following == ":"
        is_shorthand = previous in {"{", ","} and following in {",", "}"}
        if not (is_pair or is_shorthand):
            continue
        # Shorthand repeats a name already declared elsewhere in the module;
        # the pair form is where a field name is actually coined.
        if is_shorthand and token.value not in declared:
            continue
        entry = found.setdefault(token.value, {"count": 0, "first_line": token.line})
        entry["count"] = int(entry["count"]) + 1
        entry["first_line"] = min(int(entry["first_line"]), token.line)
    return found


def _external_origins(tokens: list[Token]) -> dict[str, dict[str, Any]]:
    """Hosts named in string literals, and the assets fetched from elsewhere.

    A page that pulls a font from `fonts.googleapis.com` sends every visitor's
    address to a third party before any consent dialog renders. That is a
    privacy and content-security question, and it is decided by a string in the
    source rather than by anything in a dependency manifest — so nothing else
    in this analysis would ever surface it.

    Hosts are recorded, not full URLs: the path is usually a detail and the
    origin is what a review is about.
    """

    found: dict[str, dict[str, Any]] = {}
    for token in tokens:
        if token.kind != "string":
            continue
        value = token.value.strip()
        match = ORIGIN_LITERAL.match(value)
        if match is None:
            continue
        host = match.group("host").casefold()
        # A loopback address is the local process, not a third party. The
        # hardcoded-endpoint claim already reports those. S104 is about
        # binding a listener to a wildcard address; this is reading a host out
        # of a string literal in order to exclude it.
        if host in LOOPBACK_HOSTS or host.endswith(".local"):
            continue
        entry = found.setdefault(
            host, {"count": 0, "first_line": token.line, "scheme": match.group("scheme").lower()}
        )
        entry["count"] = int(entry["count"]) + 1
        entry["first_line"] = min(int(entry["first_line"]), token.line)
    return found


def _opens_assigned_function(tokens: list[Token], brace: int) -> bool:
    """Whether the brace at ``brace`` opens a function bound to a name.

    An IIFE wrapper is invoked rather than assigned, so its body holds module
    constants. An arrow or function expression on the right of `=` holds
    locals. Both put their contents one brace deep, and only this tells them
    apart without parsing.
    """

    scan = brace - 1
    # Step back over the parameter list, if there is one.
    if scan >= 0 and tokens[scan].value == ")":
        depth = 1
        scan -= 1
        while scan >= 0 and depth:
            if tokens[scan].value == ")":
                depth += 1
            elif tokens[scan].value == "(":
                depth -= 1
            scan -= 1
    elif scan >= 1 and tokens[scan].value == ">" and tokens[scan - 1].value == "=":
        return True
    while scan >= 0 and tokens[scan].kind == "identifier" and tokens[scan].value != "function":
        scan -= 1
    if scan < 0 or tokens[scan].value != "function":
        return False
    # `const f = function () {}` is assigned; `(function () {})()` is not.
    return scan >= 1 and tokens[scan - 1].value == "="


def _exported_names(tokens: list[Token]) -> list[str]:
    """Names this module makes public, in declaration order.

    `_declarations` records every name a module introduces and cannot say
    which of them anyone outside is allowed to use, so an internal helper and
    the function the package exists to provide looked identical. For a library
    that is the difference between the surface it must not break and the parts
    it may rewrite freely.

    In an ES module the keyword is the declaration: `export` is more explicit
    than Python's `__all__`, which has to be kept in step with the code by
    hand. Both direct forms are read -- `export function x` and the
    `export { x, y as z }` list, where the exported name is the alias.

    `export * from` is deliberately not expanded. The names it forwards live in
    another file, and listing this module as their origin would attribute a
    surface to the wrong place.
    """

    exported: list[str] = []
    total = len(tokens)
    # Only a top-level `export` names something an importer can ask this
    # module for. `export namespace N { export const inner = 1 }` publishes
    # `N`, and `inner` is reached as `N.inner`; reporting a bare `inner`
    # tells a reader that `import { inner }` works, and it does not.
    # `declare module 'x' { ... }` is worse: it augments a different module
    # entirely, so its names are not this file's surface at all.
    depth = 0
    depths: list[int] = []
    for token in tokens:
        if token.kind == "punctuation" and token.value == "}":
            depth = max(0, depth - 1)
        depths.append(depth)
        if token.kind == "punctuation" and token.value == "{":
            depth += 1
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "export" or index + 1 >= total:
            continue
        if depths[index]:
            continue
        following = tokens[index + 1]
        # `export type { Foo }` is a type-only list. The keyword sits between
        # `export` and the brace, so matching only on `{` missed the whole
        # form -- and it is how TypeScript projects publish their types.
        # `export * as core from "./x"` binds a namespace object called
        # `core`. Only the bare `export * from` forwards without naming
        # anything, and treating both the same lost a real export.
        if (
            following.value == "*"
            and index + 3 < total
            and tokens[index + 2].value == "as"
            and tokens[index + 3].kind == "identifier"
        ):
            exported.append(tokens[index + 3].value)
            continue
        brace = index + 1
        if following.value == "type" and index + 2 < total and tokens[index + 2].value == "{":
            following = tokens[index + 2]
            brace = index + 2
        if following.value == "{":
            scan = brace + 1
            pending: str | None = None
            while scan < total and tokens[scan].value != "}":
                current = tokens[scan]
                if current.kind == "identifier":
                    # `export { type Foo, Bar }` marks one entry as a type. The
                    # modifier is not a name, and reporting it published an
                    # export called `type` that no module has.
                    if current.value == "type" and scan + 1 < total:
                        scan += 1
                        continue
                    # `a as b` exports b; the name before `as` is local.
                    if current.value == "as" and scan + 1 < total:
                        pending = tokens[scan + 1].value
                        scan += 2
                        continue
                    if pending is None:
                        pending = current.value
                elif current.value == "," and pending is not None:
                    exported.append(pending)
                    pending = None
                scan += 1
            if pending is not None:
                exported.append(pending)
            continue
        # `export default class Engine {}` binds `default`, not `Engine`. An
        # importer writes `import Anything from "./x"`, so renaming the class
        # breaks nobody -- and reporting `Engine` as the public name asserted a
        # compatibility promise the module does not make. Found by comparing
        # against esbuild, which reports what the module system actually binds.
        if following.value == "default":
            exported.append("default")
            continue
        # `export const { GET } = handler()` binds each destructured name.
        # Reading only `export <keyword> <identifier>` saw the brace and
        # recorded nothing.
        if (
            following.value in BINDING_KEYWORDS
            and index + 2 < total
            and tokens[index + 2].value in {"{", "["}
        ):
            closing = {"{": "}", "[": "]"}[tokens[index + 2].value]
            scan = index + 3
            while scan < total and tokens[scan].value != closing:
                current = tokens[scan]
                # `{ a: b }` binds b: the name before the colon is the key
                # being read, and the one after it is what gets exported.
                next_value = tokens[scan + 1].value if scan + 1 < total else ""
                if current.kind == "identifier" and next_value != ":":
                    exported.append(current.value)
                scan += 1
            continue
        if following.value in EXPORTABLE_KEYWORDS and index + 2 < total:
            name = tokens[index + 2]
            # `export default function ...` and `export async function ...`
            # put another keyword before the name.
            if name.value in EXPORTABLE_KEYWORDS and index + 3 < total:
                name = tokens[index + 3]
            if name.kind == "identifier" and name.value not in EXPORTABLE_KEYWORDS:
                exported.append(name.value)
    # Declaration order is kept: it is the order a reader meets them in the file.
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in exported:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _tunables(tokens: list[Token]) -> dict[str, dict[str, Any]]:
    """Named literal constants: the numbers a reader would change to retune.

    The Rust analyzer has carried these since it was written and this one did
    not, which showed up the first time both ran against a physics-heavy game.
    Its 16 modules yielded 4 claims while the tuning constants that decide how
    the whole thing feels -- gravity, friction, acceleration caps -- were
    visible in every file and recorded nowhere.

    Depth is what separates a tunable from a loop variable. A `const` at brace
    depth 0 is a module constant in an ES module, and one at depth 1 is the
    same thing inside the IIFE wrapper that buildless browser code still uses;
    below that it is a local, and this makes no claim about it.
    """

    found: dict[str, dict[str, Any]] = {}
    # Depth alone is not enough. `const f = () => { const scratch = 1; }` puts a
    # function-local at depth 1, indistinguishable from a constant inside the
    # IIFE wrapper, and reporting a scratch variable as a knob a maintainer
    # would tune is a fabricated fact rather than a missed one. Each open brace
    # therefore records whether it belongs to a function that was assigned to
    # something; the IIFE wrapper is invoked rather than assigned, so it is not.
    in_function: list[bool] = []
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind == "punctuation":
            if token.value == "{":
                in_function.append(_opens_assigned_function(tokens, index))
            elif token.value == "}" and in_function:
                in_function.pop()
            continue
        if token.kind != "identifier" or token.value != "const":
            continue
        if len(in_function) > 1 or any(in_function):
            continue
        if index + 3 >= total:
            continue
        name = tokens[index + 1]
        if name.kind != "identifier" or tokens[index + 2].value != "=":
            continue
        value = tokens[index + 3]
        sign = ""
        if value.value == "-" and index + 4 < total:
            sign, value = "-", tokens[index + 4]
        if value.kind not in {"number", "string"}:
            continue
        found[name.value] = {
            "line": name.line,
            "kind": "const",
            "value": f"{sign}{value.value}" if value.kind == "number" else value.value,
            "literal": value.kind,
        }
    return found


def _server_receivers(tokens: list[Token]) -> set[str]:
    """Names bound to something that serves requests.

    `app.get("/x", handler)` and `axios.get("/x")` are the same four tokens.
    What separates a served route from an outbound call is not the syntax but
    what the receiver is, so the receiver is resolved to its constructor rather
    than guessed at from its name. A receiver whose origin is not visible in
    this file yields no claim in either direction, which is the honest outcome:
    `client.get("/x")` could be either, and asserting one would be a coin flip
    reported as a fact.
    """

    receivers: set[str] = set()
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value not in SERVER_FACTORIES:
            continue
        # The factory must be called: `express` alone is the module.
        call = index + 1
        if call < total and tokens[call].value == ".":
            # `express.Router()` — step over the member access.
            call = index + 3 if index + 2 < total else call
        if call >= total or tokens[call].value != "(":
            continue
        # Walk back over `=`, an optional type annotation, and the binding
        # keyword to reach the name being defined.
        scan = index - 1
        while scan >= 0 and tokens[scan].value != "=":
            if tokens[scan].value in {";", "{", "}"}:
                scan = -1
                break
            scan -= 1
        name = scan - 1
        while name >= 0 and tokens[name].kind != "identifier":
            name -= 1
        if name >= 0 and tokens[name].value not in BINDING_KEYWORDS:
            receivers.add(tokens[name].value)
    return receivers


def _served_routes(tokens: list[Token], receivers: set[str]) -> list[tuple[str, str, int]]:
    """`(method, path, line)` for routes this module registers on a server."""

    found: list[tuple[str, str, int]] = []
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value not in receivers:
            continue
        if index + 4 >= total or tokens[index + 1].value != ".":
            continue
        method = tokens[index + 2]
        if method.kind != "identifier" or method.value not in SERVER_METHOD_NAMES:
            continue
        if tokens[index + 3].value != "(" or tokens[index + 4].kind != "string":
            continue
        path, exact = _request_path(tokens[index + 4].value)
        if not path.startswith("/") or not exact:
            continue
        label = "MOUNT" if method.value == "use" else method.value.upper()
        found.append((label, path, token.line))
    return found


def _next_route_path(path: str) -> str:
    """The URL a Next.js file-convention route answers on, or an empty string.

    Both router generations map a file location to a served path. This is a
    convention rather than a call, so it is the only place a route is read
    from a filename -- and it is restricted to the two directory shapes that
    define it, so an ordinary `api.ts` helper is not mistaken for an endpoint.
    """

    parts = path.split("/")
    for marker in ("pages", "app", "src"):
        if marker in parts:
            parts = parts[parts.index(marker) + 1 :]
            break
    if not parts or parts[0] != "api":
        return ""
    stem = parts[-1].rsplit(".", 1)[0]
    # `route.ts` and `index.ts` name the directory's own path rather than a
    # child of it, which is what makes `app/api/users/route.ts` serve /api/users.
    segments = parts[:-1] if stem in {"route", "index"} else [*parts[:-1], stem]
    if not segments:
        return ""
    return "/" + "/".join(segments)


def _import_aliases(tokens: list[Token]) -> dict[str, str]:
    """Local binding to original name for `import { a as b }` forms.

    `_imported_names` records the local binding, which is the right answer for
    "what does this module contribute". It is the wrong answer for counting
    hook calls: `import { useState as useLocal }` then `useLocal(0)` is a
    `useState` call, and matching the literal name undercounts it.
    """

    aliases: dict[str, str] = {}
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "as":
            continue
        if index == 0 or index + 1 >= total:
            continue
        original = tokens[index - 1]
        local = tokens[index + 1]
        if original.kind == "identifier" and local.kind == "identifier":
            aliases[local.value] = original.value
    return aliases


def _request_path(value: str) -> tuple[str, bool]:
    """A request path and whether it is fully known.

    A template literal such as `/api/player/${id}` is only knowable up to its
    first substitution. Returning the static prefix marked inexact lets the
    path be reported as a pattern; returning the whole string would assert a
    literal `${id}` segment that no server ever sees.
    """

    marker = value.find("${")
    if marker < 0:
        return value, True
    return value[:marker], False


def _imported_names(tokens: list[Token]) -> dict[str, dict[str, Any]]:
    """Which names each module contributes, not merely that it was imported.

    The import edge already records `react-dom`. It does not record that what
    comes from it is `createPortal`, and that is the difference between a
    dependency a system is built on and one it borrows a helper from.
    """

    found: dict[str, dict[str, Any]] = {}
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "import":
            continue
        names: list[str] = []
        scan = index + 1
        source: Token | None = None
        while scan < total:
            current = tokens[scan]
            if current.kind == "string":
                source = current
                break
            if current.kind == "punctuation" and current.value == ";":
                break
            if current.kind == "identifier" and current.value not in {"from", "type", "as"}:
                previous = tokens[scan - 1].value
                # `import x as y` binds `y`, so a name followed by `as` is the
                # original and the local name is what the module refers to.
                following = tokens[scan + 1].value if scan + 1 < total else ""
                if following != "as" and previous != "." and current.value not in names:
                    names.append(current.value)
            scan += 1
        if source is None or not names:
            continue
        entry = found.setdefault(source.value, {"names": [], "line": token.line})
        for name in names:
            if name not in entry["names"]:
                entry["names"].append(name)
        entry["line"] = min(int(entry["line"]), token.line)
    for entry in found.values():
        entry["names"] = sorted(entry["names"])
    return found


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
            continue
        # A bare call to a name this module neither declares nor binds is a
        # global or an import: `setTimeout`, `encodeURIComponent`, `fetch`.
        # Timers and `eval` are the reason this is worth naming separately
        # from the member chains above.
        if (
            following == "("
            and token.value not in declared
            and token.value not in JS_KEYWORDS
            and previous not in {".", "function", "class", "new"}
        ):
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
        module_names = _module_names(item.path for item in eligible)
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
            file_imports = _imported_names(file_tokens)
            file_aliases = _import_aliases(file_tokens)
            file_servers = _server_receivers(file_tokens)
            file_tunables = _tunables(file_tokens)
            file_exports = _exported_names(file_tokens)
            file_clients = {
                name
                for module_name, entry in file_imports.items()
                if module_name in CLIENT_MODULES
                for name in entry["names"]
            }
            file_served = _served_routes(file_tokens, file_servers)
            file_origins = _external_origins(file_tokens)
            file_name_index = _name_index(file_tokens)
            file_state = _module_state(file_tokens, file_declarations)
            file_env = _environment_reads(file_tokens)
            file_throws = _throw_sites(file_tokens)
            file_object_keys = _object_keys(
                file_tokens,
                frozenset(item.name.rsplit(".", 1)[-1] for item in file_declarations),
            )
            file_references = _references(
                file_tokens,
                frozenset(
                    {item.name.rsplit(".", 1)[-1] for item in file_declarations}
                    | _parameter_names(file_tokens)
                ),
            )
            module = module_names[file_record.path]
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
                        **({"imported_names": file_imports} if file_imports else {}),
                        **({"external_origins": file_origins} if file_origins else {}),
                        **({"object_keys": file_object_keys} if file_object_keys else {}),
                        **({"tunables": file_tunables} if file_tunables else {}),
                        **({"name_index": file_name_index} if file_name_index else {}),
                    },
                )
            )

            fetch_evidence: list[str] = []
            requested: list[tuple[str, bool, int, str]] = []
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
                    # The call site was already counted. What was missing is
                    # *which* endpoint it calls, which is the half that lets a
                    # caller be joined to the route that serves it.
                    argument = file_tokens[index + 2] if index + 2 < len(file_tokens) else None
                    if argument is not None and argument.kind == "string":
                        called, exact = _request_path(argument.value)
                        if called.startswith(("/", "http://", "https://")):
                            requested.append((called, exact, token.line, receipt.evidence_id))

                # `axios.get("/x")` is the same shape as `app.get("/x")`, so the
                # receiver decides again: a client module named in an import is
                # an outbound call, a server factory is a route, and anything
                # else is left alone.
                if (
                    token.value in file_clients
                    and index + 4 < len(file_tokens)
                    and file_tokens[index + 1].value == "."
                    and file_tokens[index + 2].value in SERVER_METHOD_NAMES
                    and file_tokens[index + 3].value == "("
                    and file_tokens[index + 4].kind == "string"
                ):
                    called, exact = _request_path(file_tokens[index + 4].value)
                    if called.startswith(("/", "http://", "https://")):
                        receipt = add_evidence(
                            file_record.path,
                            token.line,
                            token.end_line,
                            module,
                            "http_client_call",
                            file_record.sha256,
                        )
                        requested.append((called, exact, token.line, receipt.evidence_id))
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

                hook_name = file_aliases.get(token.value, token.value)
                if hook_name in REACT_HOOKS and _call_open_paren(file_tokens, index) is not None:
                    receipt = add_evidence(
                        file_record.path,
                        token.line,
                        token.end_line,
                        module,
                        "react_hook_call",
                        file_record.sha256,
                    )
                    hook_evidence[hook_name].append(receipt.evidence_id)

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

            if file_exports:
                surface = add_evidence(
                    file_record.path, 1, 1, module, "public_api", file_record.sha256
                )
                shown = ", ".join(sorted(file_exports)[:12])
                add_claim(
                    f"{module} exports {len(file_exports)} name(s): {shown}"
                    f"{'...' if len(file_exports) > 12 else ''}. Renaming or removing one is a "
                    "breaking change for every importer.",
                    "public_api",
                    "high",
                    [surface.evidence_id],
                    file_record.path,
                )

            if fetch_evidence:
                add_claim(
                    f"{file_record.path} contains {len(fetch_evidence)} fetch call sites.",
                    "http_client_inventory",
                    "high",
                    fetch_evidence,
                    file_record.path,
                )

            for method, served_path, served_line in file_served:
                mounted = method == "MOUNT"
                served_receipt = add_evidence(
                    file_record.path,
                    served_line,
                    served_line,
                    module,
                    "route_mount" if mounted else "http_route",
                    file_record.sha256,
                )
                add_claim(
                    f"{served_path} mounts a sub-router in {module}, so paths it contains are "
                    "served beneath this prefix."
                    if mounted
                    else f"{method} {served_path} is registered as a route in {module}.",
                    "route_mount" if mounted else "http_route",
                    "medium" if mounted else "high",
                    [served_receipt.evidence_id],
                    file_record.path,
                )

            next_path = _next_route_path(file_record.path)
            if next_path:
                convention_receipt = add_evidence(
                    file_record.path, 1, 1, module, "http_route", file_record.sha256
                )
                add_claim(
                    f"{next_path} is served by file convention: this module's location under an "
                    "api directory is what registers it, so no call site declares it.",
                    "http_route",
                    "high",
                    [convention_receipt.evidence_id],
                    file_record.path,
                )

            # One claim per distinct endpoint, not per call site: the same path
            # requested from three components is one edge of the system, and
            # three claims saying so would inflate the count without adding a
            # fact.
            by_path: dict[tuple[str, bool], list[str]] = {}
            for called, exact, _line, evidence_id in requested:
                by_path.setdefault((called, exact), []).append(evidence_id)
            for (called, exact), receipts in sorted(by_path.items()):
                shape = (
                    f"{called} is requested by {module}"
                    if exact
                    else f"{called} begins a request path built by {module}, whose remaining "
                    "segments are interpolated at run time"
                )
                add_claim(
                    f"{shape}; the server side of this call is whichever route matches it.",
                    "http_client_route" if exact else "http_client_route_prefix",
                    "high" if exact else "medium",
                    receipts,
                    file_record.path,
                )

            for state_name, state_line in file_state:
                state_receipt = add_evidence(
                    file_record.path,
                    state_line,
                    state_line,
                    f"{module}.{state_name}",
                    "process_local_state",
                    file_record.sha256,
                )
                add_claim(
                    (
                        f"{module}.{state_name} is a module-scope container written to while "
                        "the process runs; its contents are process-local, so a second "
                        "instance of this program observes none of them."
                    ),
                    "process_local_state",
                    "high",
                    [state_receipt.evidence_id],
                    file_record.path,
                )

            for setting, setting_line in sorted(file_env.items(), key=lambda pair: pair[1]):
                env_receipt = add_evidence(
                    file_record.path,
                    setting_line,
                    setting_line,
                    module,
                    "environment_setting",
                    file_record.sha256,
                )
                add_claim(
                    f"{module} reads environment setting {setting}.",
                    "environment_setting",
                    "medium",
                    [env_receipt.evidence_id],
                    file_record.path,
                )

            if file_throws:
                thrown = ", ".join(sorted(file_throws))
                throw_receipt = add_evidence(
                    file_record.path,
                    min(file_throws.values()),
                    min(file_throws.values()),
                    module,
                    "failure_surface",
                    file_record.sha256,
                )
                add_claim(
                    (
                        f"{module} throws {len(file_throws)} distinct type(s): {thrown}. "
                        "A caller that does not catch them sees them propagate."
                    ),
                    "failure_surface",
                    "medium",
                    [throw_receipt.evidence_id],
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
