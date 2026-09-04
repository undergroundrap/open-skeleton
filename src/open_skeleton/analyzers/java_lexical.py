# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Lexical reader for Java declarations.

Java is read rather than parsed here, for the same reason Rust and TypeScript
are: a parser for the whole grammar is a project of its own, and almost every
fact worth stating about a codebase lives in its declarations.

What makes Java worth doing next is that its declarations have an exact
reference. `javac -Xprint` emits package, type kind, supertypes,
fully-qualified signatures and precise modifiers, and it does so even when
imports do not resolve -- so this reader can be differentially tested against
a real compiler on any checkout, without a build.

That reference has one silent limit, and it decides how this module is
structured. With an incomplete classpath `javac -Xprint` drops every
annotation from its output while reporting the errors only on stderr, and
exits zero. `@RestController` and `@GetMapping("/health")` simply vanish. So
the declaration half of this reader is oracle-verified and the annotation half
cannot be: routes live in annotations, and annotations are exactly what the
reference loses. Route claims are fixture-tested, and nothing here implies a
compiler agreed with them.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from open_skeleton.analyzers.base import declares_a_number
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

IDENTIFIER_START = re.compile(r"[A-Za-z_$]")
IDENTIFIER_BODY = re.compile(r"[A-Za-z0-9_$]")

# `class`, `interface` and `enum` are reserved; `record` is contextual and is
# only a declaration when a name and `(` follow it, so it is matched by shape
# rather than by keyword.
TYPE_KEYWORDS = frozenset({"class", "interface", "enum"})
MODIFIERS = frozenset(
    {
        "public",
        "protected",
        "private",
        "static",
        "final",
        "abstract",
        "native",
        "synchronized",
        "transient",
        "volatile",
        "strictfp",
        "default",
        "sealed",
        "non",
    }
)
# Names that can precede `(` without introducing a method.
CONTROL_KEYWORDS = frozenset(
    {"if", "for", "while", "switch", "catch", "return", "new", "throw", "synchronized", "do"}
)


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    line: int


def _read_text_block(source: str, index: int, length: int) -> tuple[int, int]:
    """Index and newline count just past a `\"\"\"` text block.

    A text block spans lines and may contain unescaped quotes, so treating it
    as three empty strings makes the tokenizer read its contents as code. The
    equivalent defect in the TypeScript reader -- a regex containing a quote --
    swallowed the remainder of every file that had one.
    """

    cursor = index + 3
    newlines = 0
    while cursor < length:
        if source[cursor] == "\\":
            newlines += source[cursor + 1 : cursor + 2] == "\n"
            cursor += 2
            continue
        if source.startswith('"""', cursor):
            return cursor + 3, newlines
        newlines += source[cursor] == "\n"
        cursor += 1
    return length, newlines


def _read_quoted(source: str, index: int, length: int, quote: str) -> int:
    """Index just past a single- or double-quoted literal, honouring escapes."""

    cursor = index + 1
    while cursor < length:
        character = source[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == quote:
            return cursor + 1
        if character == "\n":
            return cursor
        cursor += 1
    return length


def tokenize(source: str) -> list[Token]:
    """Tokenize Java outside comments and literals.

    Literals are emitted rather than discarded. A route path is a string, and
    the Rust reader once dropped strings entirely, which made every route in
    the crate unreachable while looking like a clean tokenizer.
    """

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
            # Java block comments do not nest: the first `*/` closes it, and
            # treating them as nesting would swallow code after a comment
            # that merely mentions `/*`.
            end = source.find("*/", index + 2)
            stop = length if end < 0 else end + 2
            line += source.count("\n", index, stop)
            index = stop
            continue
        if source.startswith('"""', index):
            stop, newlines = _read_text_block(source, index, length)
            tokens.append(Token("string", source[index:stop], line))
            line += newlines
            index = stop
            continue
        if character == '"':
            stop = _read_quoted(source, index, length, '"')
            tokens.append(Token("string", source[index + 1 : max(stop - 1, index + 1)], line))
            index = stop
            continue
        if character == "'":
            stop = _read_quoted(source, index, length, "'")
            tokens.append(Token("char", source[index:stop], line))
            index = stop
            continue
        if character == "@" and index + 1 < length and IDENTIFIER_START.match(source[index + 1]):
            cursor = index + 1
            while cursor < length and (
                IDENTIFIER_BODY.match(source[cursor]) or source[cursor] == "."
            ):
                cursor += 1
            name = source[index + 1 : cursor]
            # `@interface` declares an annotation type; it is not a use of an
            # annotation called `interface`. Absorbing the keyword hid every
            # such declaration -- all twelve in `java.lang` alone -- because
            # the `interface` token the type reader looks for was gone.
            if name == "interface":
                tokens.append(Token("punctuation", "@", line))
                tokens.append(Token("identifier", "interface", line))
                index = cursor
                continue
            tokens.append(Token("annotation", name, line))
            index = cursor
            continue
        if character.isdigit():
            cursor = index
            while cursor < length and (source[cursor].isalnum() or source[cursor] in {"_", "."}):
                cursor += 1
            tokens.append(Token("number", source[index:cursor], line))
            index = cursor
            continue
        if IDENTIFIER_START.match(character):
            cursor = index
            while cursor < length and IDENTIFIER_BODY.match(source[cursor]):
                cursor += 1
            tokens.append(Token("identifier", source[index:cursor], line))
            index = cursor
            continue
        tokens.append(Token("punctuation", character, line))
        index += 1
    return tokens


def package_name(tokens: list[Token]) -> str:
    """The declared package, or an empty string for the default package."""

    for position, token in enumerate(tokens):
        if token.kind == "identifier" and token.value == "package":
            parts: list[str] = []
            for follower in tokens[position + 1 :]:
                if follower.kind == "punctuation" and follower.value == ";":
                    break
                if follower.kind == "identifier":
                    parts.append(follower.value)
            return ".".join(parts)
        if token.kind == "identifier" and token.value in TYPE_KEYWORDS:
            break
    return ""


def imported_types(tokens: list[Token]) -> list[tuple[str, int]]:
    """Every `import` target with the line that declares it."""

    found: list[tuple[str, int]] = []
    for position, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "import":
            continue
        parts: list[str] = []
        for follower in tokens[position + 1 :]:
            if follower.kind == "punctuation" and follower.value == ";":
                break
            if follower.kind == "identifier" and follower.value != "static":
                parts.append(follower.value)
            elif follower.kind == "punctuation" and follower.value == "*":
                parts.append("*")
        if parts:
            found.append((".".join(parts), token.line))
    return found


def _annotations_before(tokens: list[Token], start: int) -> tuple[str, ...]:
    """Annotation names attached to the declaration beginning at ``start``."""

    found: list[str] = []
    cursor = start - 1
    depth = 0
    while cursor >= 0:
        token = tokens[cursor]
        if token.kind == "punctuation" and token.value == ")":
            depth += 1
        elif token.kind == "punctuation" and token.value == "(":
            depth -= 1
        elif depth == 0 and token.kind == "annotation":
            found.append(token.value)
        elif (
            depth == 0
            and not (token.kind == "identifier" and token.value in MODIFIERS)
            and not (token.kind == "punctuation" and token.value in {"]", "["})
        ):
            break
        cursor -= 1
    return tuple(reversed(found))


@dataclass(frozen=True, slots=True)
class JavaType:
    """One declared type and where its body sits."""

    name: str
    kind: str
    line: int
    modifiers: tuple[str, ...]
    annotations: tuple[str, ...]
    supertypes: tuple[str, ...]
    depth: int
    local: bool = False


def declared_types(tokens: list[Token]) -> list[JavaType]:
    """Every type declaration, with nested types qualified by their owner.

    A type declared inside a method body is marked ``local`` rather than
    dropped. Java allows it, and it is genuinely different from a member: a
    local class is not reachable by any qualified name, so treating one as a
    member invents `Outer.Local` as part of a public surface it never joins.
    `javac -Xprint` does not print them at all, which is the same judgement.
    """

    found: list[JavaType] = []
    stack: list[tuple[str, int, bool]] = []
    depth = 0
    position = 0
    total = len(tokens)
    while position < total:
        token = tokens[position]
        if token.kind == "punctuation" and token.value == "{":
            depth += 1
            position += 1
            continue
        if token.kind == "punctuation" and token.value == "}":
            depth -= 1
            while stack and stack[-1][1] > depth:
                stack.pop()
            position += 1
            continue
        if token.kind != "identifier":
            position += 1
            continue
        kind = _type_kind(tokens, position)
        if kind is None:
            position += 1
            continue
        name_token = tokens[position + 1] if position + 1 < total else None
        if name_token is None or name_token.kind != "identifier":
            position += 1
            continue
        # A member sits directly in its owner's body. Anything deeper is
        # inside a method, constructor or initializer.
        #
        # Locality is inherited. `JSlider` declares a local class inside a
        # method, and that class declares a member of its own: the member sits
        # correctly in its owner's body and is still reachable by no qualified
        # name, because its owner is not. Judging each type only against its
        # immediate owner published `JSlider.SmartHashtable.LabelUIResource`,
        # the one disagreement in 12,000 files of the JDK.
        owner_body_depth = stack[-1][1] if stack else 0
        owner_is_local = stack[-1][2] if stack else False
        is_local = depth > owner_body_depth or owner_is_local
        owner = ".".join(name for name, _, _ in stack)
        qualified = f"{owner}.{name_token.value}" if owner else name_token.value
        found.append(
            JavaType(
                name=qualified,
                kind=kind,
                line=token.line,
                modifiers=_modifiers_before(tokens, position),
                annotations=_annotations_before(tokens, position),
                supertypes=_supertypes_after(tokens, position + 2),
                depth=depth,
                local=is_local,
            )
        )
        stack.append((name_token.value, depth + 1, is_local))
        position += 2
    return found


MAX_NAMED = 12
MAX_MESSAGE_CHARS = 60
MAX_ENUM_CONSTANTS = 64


def declared_enums(tokens: list[Token]) -> dict[str, dict[str, object]]:
    """Enum constants, which are how Java declares a closed set of values.

    Python states a vocabulary with a frozenset, TypeScript with a union of
    string literals and Rust with an enum; all three are read. Java states one
    the same way Rust does and none of it was read, so `java.util.concurrent`
    could declare every unit of time it understands and a specification of it
    would name none of them.

    Constants come before the first `;` in an enum body, which is what
    separates them from the fields and methods that may follow. A constant
    carrying arguments -- `NANOSECONDS(TimeUnit.NANO_SCALE)` -- is still one
    constant, and the arguments are its construction rather than more members.
    """

    found: dict[str, dict[str, object]] = {}
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "enum" or index + 2 >= total:
            continue
        name = tokens[index + 1]
        if name.kind != "identifier":
            continue
        cursor = index + 2
        while cursor < total and tokens[cursor].value != "{":
            # `enum X implements Y {` puts a supertype list before the body.
            if tokens[cursor].value in {";", "}"}:
                break
            cursor += 1
        if cursor >= total or tokens[cursor].value != "{":
            continue

        constants: list[str] = []
        depth = 0
        expecting = True
        while cursor < total:
            current = tokens[cursor]
            if current.kind == "punctuation" and current.value in {"{", "(", "["}:
                depth += 1
            elif current.kind == "punctuation" and current.value in {"}", ")", "]"}:
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1 and current.kind == "punctuation" and current.value == ";":
                # The constants are done; fields and methods follow.
                break
            elif depth == 1 and current.kind == "punctuation" and current.value == ",":
                expecting = True
            elif depth == 1 and current.kind == "identifier" and expecting:
                constants.append(current.value)
                expecting = False
            cursor += 1
        if 2 <= len(constants) <= MAX_ENUM_CONSTANTS:
            found[name.value] = {"members": constants, "line": name.line}
    return found


def declared_constants(tokens: list[Token]) -> dict[str, dict[str, object]]:
    """`static final` fields whose value is written out as a literal.

    These are the numbers and strings a reader changes to alter behaviour, and
    the equivalent index exists for Python, TypeScript and Rust.
    `ForkJoinPool` states its worker ceiling and its default spare count this
    way, and neither was recorded.

    A value that is computed -- `Integer.SIZE - 3` -- is not a literal, and
    reporting the first token of an expression as the value would state a
    number the program never uses.
    """

    found: dict[str, dict[str, object]] = {}
    total = len(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "static":
            continue
        cursor = index + 1
        if cursor >= total or tokens[cursor].value != "final":
            continue
        # Step over the type, which may be qualified or generic, to the name.
        name_index = -1
        scan = cursor + 1
        depth = 0
        while scan < total:
            current = tokens[scan]
            if current.value in {"<", "["}:
                depth += 1
            elif current.value in {">", "]"}:
                depth -= 1
            elif depth == 0 and current.kind == "identifier":
                following = tokens[scan + 1] if scan + 1 < total else None
                if following is not None and following.value == "=":
                    name_index = scan
                    break
            elif depth == 0 and current.value in {";", "{", "("}:
                break
            scan += 1
        if name_index < 0 or name_index + 2 >= total:
            continue
        value = tokens[name_index + 2]
        after = tokens[name_index + 3] if name_index + 3 < total else None
        if after is None or after.value != ";":
            continue
        if value.kind not in {"number", "string"}:
            continue
        found[tokens[name_index].value] = {"value": value.value, "line": value.line}
    return found


@dataclass(frozen=True, slots=True)
class JavaMember:
    """A method or field declared directly in a type body."""

    owner: str
    name: str
    kind: str
    line: int
    modifiers: tuple[str, ...]
    annotations: tuple[str, ...]
    # Annotation name to its first string argument, which is where a route
    # path lives: `@GetMapping("/health")`. Empty when an annotation takes no
    # arguments or takes no string.
    annotation_arguments: tuple[tuple[str, str], ...] = ()
    # The type a field was declared with, rendered from its own tokens:
    # `Map<String,Integer>`, `int[]`, `java.io.File`. Empty for a method,
    # whose return type this reader has no use for yet.
    declared_type: str = ""


def declared_members(tokens: list[Token]) -> list[JavaMember]:
    """Methods and fields declared directly in a type body.

    Only members are returned. A local variable sits inside a method body and
    is not part of any declared surface, so depth decides membership exactly
    as it does for nested types.
    """

    found: list[JavaMember] = []
    stack: list[tuple[str, int]] = []
    depth = 0
    head: list[int] = []
    position = 0
    total = len(tokens)
    while position < total:
        token = tokens[position]
        if token.kind == "punctuation" and token.value in {"{", "}", ";"}:
            if token.value == "{":
                kind = _type_kind_at_head(tokens, head)
                if kind is not None:
                    name = _head_type_name(tokens, head)
                    if name is not None:
                        stack.append((name, depth + 1))
                elif head and stack and depth == stack[-1][1]:
                    member = _classify(tokens, head, stack[-1][0])
                    if member is not None:
                        found.append(member)
                depth += 1
            elif token.value == "}":
                depth -= 1
                while stack and stack[-1][1] > depth:
                    stack.pop()
            elif head and stack and depth == stack[-1][1]:
                member = _classify(tokens, head, stack[-1][0])
                if member is not None:
                    found.append(member)
            head = []
            position += 1
            continue
        head.append(position)
        position += 1
    return found


def _type_kind_at_head(tokens: list[Token], head: list[int]) -> str | None:
    for index in head:
        if tokens[index].kind == "identifier" and _type_kind(tokens, index) is not None:
            return _type_kind(tokens, index)
    return None


def _head_type_name(tokens: list[Token], head: list[int]) -> str | None:
    for offset, index in enumerate(head):
        if tokens[index].kind == "identifier" and _type_kind(tokens, index) is not None:
            following = head[offset + 1] if offset + 1 < len(head) else None
            if following is not None and tokens[following].kind == "identifier":
                return tokens[following].value
            return None
    return None


def _classify(tokens: list[Token], head: list[int], owner: str) -> JavaMember | None:
    """Read one declaration head as a method or a field."""

    annotations: list[str] = []
    arguments: list[tuple[str, str]] = []
    # An annotation's own argument list is not part of the declaration.
    # Leaving it in made the scan below find `@GetMapping(` before the method's
    # parameter list and reject the whole head, so every annotated route
    # method disappeared while unannotated ones were read correctly.
    rest: list[int] = []
    position = 0
    while position < len(head):
        token = tokens[head[position]]
        if token.kind != "annotation":
            rest.append(head[position])
            position += 1
            continue
        annotations.append(token.value)
        position += 1
        if position < len(head) and tokens[head[position]].value == "(":
            depth = 0
            first_string: str | None = None
            while position < len(head):
                current = tokens[head[position]]
                if current.kind == "punctuation" and current.value == "(":
                    depth += 1
                elif current.kind == "punctuation" and current.value == ")":
                    depth -= 1
                    if depth == 0:
                        position += 1
                        break
                elif current.kind == "string" and first_string is None:
                    first_string = current.value
                position += 1
            if first_string is not None:
                arguments.append((token.value, first_string))
    head = rest
    # Everything after `=` initializes the declaration rather than declaring
    # anything, and `new Cart()` in that position was being read as a
    # parameter list.
    for offset, index in enumerate(head):
        if tokens[index].kind == "punctuation" and tokens[index].value == "=":
            head = head[:offset]
            break
    modifiers = [
        tokens[index].value
        for index in head
        if tokens[index].kind == "identifier" and tokens[index].value in MODIFIERS
    ]
    # A method head carries a parameter list, and the name is the identifier
    # immediately before it. `if (...)` and `new Foo(...)` also carry one, so
    # a control keyword in that position rules the head out.
    for offset, index in enumerate(head):
        token = tokens[index]
        if token.kind != "punctuation" or token.value != "(":
            continue
        if offset == 0:
            return None
        previous = tokens[head[offset - 1]]
        if previous.kind != "identifier" or previous.value in CONTROL_KEYWORDS:
            return None
        return JavaMember(
            owner=owner,
            name=previous.value,
            kind="method",
            line=previous.line,
            modifiers=tuple(modifiers),
            annotations=tuple(annotations),
            annotation_arguments=tuple(arguments),
        )
    # A field head ends at its name or at the `=` that initializes it.
    names = [
        index
        for index in head
        if tokens[index].kind == "identifier" and tokens[index].value not in MODIFIERS
    ]
    # The head already ends before any `=`, so every name here is part of the
    # declaration.
    if len(names) < 2:
        return None
    last = tokens[names[-1]]
    # The type is what sits between the first word that is not a modifier and
    # the name: every token, punctuation included, so `Map<String,Integer>`
    # and `int[]` survive. Joined without spaces, the way the Rust reader
    # renders a struct field, because one panel draws both.
    declared = "".join(tokens[index].value for index in head if names[0] <= index < names[-1])[:80]
    return JavaMember(
        owner=owner,
        name=last.value,
        kind="field",
        line=last.line,
        modifiers=tuple(modifiers),
        annotations=tuple(annotations),
        annotation_arguments=tuple(arguments),
        declared_type=declared,
    )


def _type_kind(tokens: list[Token], position: int) -> str | None:
    """The kind a type keyword at ``position`` introduces, if it is one."""

    token = tokens[position]
    if token.value in TYPE_KEYWORDS:
        # `interface` after `@` is an annotation type declaration.
        previous = tokens[position - 1] if position else None
        if (
            token.value == "interface"
            and previous is not None
            and previous.kind == "punctuation"
            and previous.value == "@"
        ):
            return "annotation_type"
        return token.value
    if token.value != "record":
        return None
    # `record` is contextual: it names a type only when a name and a
    # parameter list follow. Elsewhere it is an ordinary identifier, and
    # treating it as a keyword invents a type from `var record = ...`.
    following = tokens[position + 1 : position + 3]
    if (
        len(following) == 2
        and following[0].kind == "identifier"
        and following[1].kind == "punctuation"
        and following[1].value in {"(", "<"}
    ):
        return "record"
    return None


def _modifiers_before(tokens: list[Token], start: int) -> tuple[str, ...]:
    found: list[str] = []
    cursor = start - 1
    while cursor >= 0:
        token = tokens[cursor]
        if token.kind == "identifier" and token.value in MODIFIERS:
            found.append(token.value)
            cursor -= 1
            continue
        break
    return tuple(reversed(found))


def _supertypes_after(tokens: list[Token], start: int) -> tuple[str, ...]:
    """Names named by `extends` and `implements` before the body opens."""

    found: list[str] = []
    collecting = False
    generic_depth = 0
    # A qualified supertype is one name written with dots in it. Treating each
    # segment as its own supertype turned `implements
    # java.security.PrivilegedAction` into three, two of which -- `java` and
    # `security` -- are packages that no type can implement. Fabricated
    # supertypes are worse than missing ones: they carry receipts, they reach
    # the specification as verified claims, and they add `implements java`
    # edges that capability clustering then reasons over.
    continuing = False
    for token in tokens[start:]:
        if token.kind == "punctuation":
            if token.value == "{" and generic_depth == 0:
                break
            if token.value == "<":
                generic_depth += 1
            elif token.value == ">":
                generic_depth = max(0, generic_depth - 1)
            elif token.value == "." and generic_depth == 0:
                continuing = True
                continue
            elif token.value == "," and generic_depth == 0:
                continuing = False
            continue
        if token.kind != "identifier":
            continue
        if token.value in {"extends", "implements"}:
            collecting = True
            continuing = False
            continue
        if token.value == "permits":
            collecting = False
            continue
        # Only the outermost names are supertypes; a generic argument such as
        # the `String` of `implements List<String>` is not one.
        if collecting and generic_depth == 0:
            # Every class extends `Object`, so writing it says nothing that is
            # not already true of every type in the language. Six classes in
            # the JDK spell it out and `javac -Xprint` normalizes it away;
            # keeping it would put a vacuously true row in the one section a
            # reader consults to learn what a type can stand in for.
            if not continuing and token.value == "Object":
                continue
            if continuing and found:
                found[-1] = f"{found[-1]}.{token.value}"
            else:
                found.append(token.value)
            continuing = False
    return tuple(name for name in dict.fromkeys(found) if name != "java.lang.Object")


def enum_constants(tokens: list[Token]) -> list[tuple[str, str, int]]:
    """Every enum constant, as (owning type, constant, line).

    Enum constants are the whole point of an enum and were invisible here.
    Worse, one carrying arguments -- `RED("r")` -- has the shape of a method
    declaration, so the member reader called it a method named `RED` and then
    consumed the rest of the list, losing `GREEN` and `BLUE` entirely. A
    public surface that omits an enum's constants omits the only part of it
    callers name.

    Constants sit at the start of the body and end at the first `;`, so this
    reads that region and stops.
    """

    found: list[tuple[str, str, int]] = []
    stack: list[tuple[str, str, int]] = []
    depth = 0
    collecting_at: int | None = None
    expect_constant = False
    position = 0
    total = len(tokens)
    while position < total:
        token = tokens[position]
        if token.kind == "punctuation" and token.value == "{":
            depth += 1
            if stack and stack[-1][1] == "enum" and stack[-1][2] == depth and collecting_at is None:
                collecting_at = depth
                expect_constant = True
            position += 1
            continue
        if token.kind == "punctuation" and token.value == "}":
            depth -= 1
            if collecting_at is not None and depth < collecting_at:
                collecting_at = None
                expect_constant = False
            while stack and stack[-1][2] > depth:
                stack.pop()
            position += 1
            continue
        if collecting_at is not None and depth == collecting_at:
            if token.kind == "punctuation" and token.value == ";":
                # The constant list is over; everything after is ordinary
                # members and must not be read as constants.
                collecting_at = None
                expect_constant = False
                position += 1
                continue
            if token.kind == "punctuation" and token.value == ",":
                expect_constant = True
                position += 1
                continue
            if token.kind == "annotation":
                position += 1
                continue
            if expect_constant and token.kind == "identifier":
                found.append((stack[-1][0], token.value, token.line))
                expect_constant = False
                position += 1
                continue
            if token.kind == "punctuation" and token.value == "(":
                # Skip a constant's constructor arguments whole.
                inner = 0
                while position < total:
                    current = tokens[position]
                    if current.kind == "punctuation" and current.value == "(":
                        inner += 1
                    elif current.kind == "punctuation" and current.value == ")":
                        inner -= 1
                        if inner == 0:
                            position += 1
                            break
                    position += 1
                continue
            position += 1
            continue
        if token.kind == "identifier":
            kind = _type_kind(tokens, position)
            if (
                kind is not None
                and position + 1 < total
                and tokens[position + 1].kind == "identifier"
            ):
                stack.append((tokens[position + 1].value, kind, depth + 1))
                position += 2
                continue
        position += 1
    return found


def record_components(tokens: list[Token]) -> list[tuple[str, str, str, int]]:
    """Every record component, as (owning record, component, type, line).

    A record's components are its public accessors: `record Point(int x, int
    y)` publishes `x()` and `y()`, and renaming one breaks every caller. They
    are declared in the header rather than the body, so a reader that only
    walks the body reports a record as exposing whatever else it happens to
    declare and nothing of what it is for.
    """

    found: list[tuple[str, str, str, int]] = []
    position = 0
    total = len(tokens)
    while position < total:
        token = tokens[position]
        if token.kind != "identifier" or _type_kind(tokens, position) != "record":
            position += 1
            continue
        cursor = position + 2
        # A generic record puts its type parameters before the component list.
        if cursor < total and tokens[cursor].kind == "punctuation" and tokens[cursor].value == "<":
            angle = 0
            while cursor < total:
                current = tokens[cursor]
                if current.kind == "punctuation" and current.value == "<":
                    angle += 1
                elif current.kind == "punctuation" and current.value == ">":
                    angle -= 1
                    if angle == 0:
                        cursor += 1
                        break
                cursor += 1
        if not (cursor < total and tokens[cursor].value == "("):
            position += 1
            continue
        owner = tokens[position + 1].value
        depth = 0
        # Every token of the component, not only its identifiers: the type is
        # the part before the name, and `List<String>` loses its meaning if
        # the punctuation is dropped on the way past.
        group: list[Token] = []

        def component(collected: list[Token], record: str = owner) -> None:
            if not collected or collected[-1].kind != "identifier":
                return
            last = collected[-1]
            declared = "".join(item.value for item in collected[:-1])[:80]
            found.append((record, last.value, declared, last.line))

        while cursor < total:
            current = tokens[cursor]
            if current.kind == "punctuation" and current.value in {"(", "<"}:
                depth += 1
                if depth > 1:
                    group.append(current)
            elif current.kind == "punctuation" and current.value in {")", ">"}:
                depth -= 1
                if depth == 0:
                    # The component's name is the last identifier of its
                    # declaration, after the type and any generic arguments.
                    component(group)
                    break
                group.append(current)
            elif current.kind == "punctuation" and current.value == "," and depth == 1:
                component(group)
                group = []
                cursor += 1
                continue
            elif depth >= 1 and (current.kind == "identifier" or current.kind == "punctuation"):
                group.append(current)
            cursor += 1
        position = cursor + 1
    return found


def declared_shapes(tokens: list[Token]) -> dict[str, dict[str, Any]]:
    """What each declared type holds, in the shape `model_fields` already has.

    Python annotates a class and Rust names a struct's fields; Java writes the
    same fact as a record header or a set of instance fields, and this reader
    counted both for the public surface and then discarded the types. A
    document that names `Order` without saying it holds an identifier and a
    total describes a container by its label.

    A `static` field is excluded. It belongs to the class rather than to any
    instance, so it is a constant or shared state -- both already reported --
    and putting it here would describe a shape nobody constructs.

    `required` is set only where the language settles it. A record component
    must be supplied at construction. An instance field's requirement depends
    on which constructors exist, so it is left unstated rather than guessed:
    the panel renders an absent requirement as a dash.
    """

    bases = {item.name.rsplit(".", 1)[-1]: list(item.supertypes) for item in declared_types(tokens)}
    fields: dict[str, list[dict[str, Any]]] = {}
    first_line: dict[str, int] = {}

    for owner, name, declared, line in record_components(tokens):
        fields.setdefault(owner, []).append(
            {"name": name, "annotation": declared, "required": True, "line": line}
        )
        first_line.setdefault(owner, line)

    for member in declared_members(tokens):
        if member.kind != "field" or "static" in member.modifiers or not member.declared_type:
            continue
        fields.setdefault(member.owner, []).append(
            {"name": member.name, "annotation": member.declared_type, "line": member.line}
        )
        first_line.setdefault(member.owner, member.line)

    return {
        owner: {
            "fields": declared,
            "line": first_line[owner],
            "bases": bases.get(owner, []),
        }
        for owner, declared in fields.items()
        if declared
    }


ANALYZER_NAME = "java-lexical"
ANALYZER_VERSION = "java-lexical/v1"
ELIGIBLE_LANGUAGES = frozenset({"Java"})

# Annotation names that declare an HTTP route, mapped to the method they
# imply. `RequestMapping` and JAX-RS `Path` name no method on their own, so
# they are reported without one rather than guessed at.
ROUTE_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": "",
    "Path": "",
}
TEST_ANNOTATIONS = frozenset({"Test", "ParameterizedTest", "RepeatedTest", "TestFactory"})
PUBLIC_KINDS = frozenset({"class", "interface", "enum", "record", "annotation_type"})


def _dotted_name(tokens: list[Token], start: int) -> tuple[str, int]:
    """A `java.io.IOException`-shaped name from `start`, and the index after it."""

    parts: list[str] = []
    cursor = start
    while cursor < len(tokens) and tokens[cursor].kind == "identifier":
        parts.append(tokens[cursor].value)
        cursor += 1
        if (
            cursor < len(tokens)
            and tokens[cursor].kind == "punctuation"
            and tokens[cursor].value == "."
        ):
            cursor += 1
            continue
        break
    return ".".join(parts), cursor


ENVIRONMENT_CALLS = {
    "getenv": "environment setting",
    "getProperty": "system property",
}


def environment_reads(tokens: list[Token]) -> list[tuple[str, str, int]]:
    """Settings a file reads from outside itself, as `(name, kind, line)`.

    `System.getProperty` outnumbers `System.getenv` fifty to nine across
    `java.base`, and the two are not interchangeable: a property is supplied
    with `-D` on the command line or set by the program, an environment
    setting by whatever starts the process. Reporting both as "environment"
    would tell an operator to set the wrong thing, so each is named.

    The receiver must be `System`. A `Properties` object also answers
    `getProperty`, and that reads a file the program loaded rather than
    something the machine supplies.

    `System.getProperty(name)` passes a variable and yields no string token.
    Its value is not knowable without running the program, so it is not
    recorded -- the same rule the Rust reader applies to a name built at
    run time.
    """

    found: list[tuple[str, str, int]] = []
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value not in ENVIRONMENT_CALLS:
            continue
        if index < 2 or index + 2 >= len(tokens):
            continue
        if tokens[index - 1].value != "." or tokens[index - 2].value != "System":
            continue
        if tokens[index + 1].value != "(" or tokens[index + 2].kind != "string":
            continue
        name = tokens[index + 2].value.strip()
        if name:
            found.append((name, ENVIRONMENT_CALLS[token.value], token.line))
    return found


def _simple_name(name: str) -> str:
    """`java.io.IOException` and `IOException` are one type, so name it once.

    `ArrayBlockingQueue` writes the clause both ways, three methods apart, and
    counting them separately reported a file failing with two types where it
    fails with one. Java forbids two imports sharing a simple name, so the
    last segment identifies the type within a file.
    """

    return name.rsplit(".", 1)[-1]


def _literal_argument(tokens: list[Token], cursor: int) -> str | None:
    """The message, when the whole constructor argument is one string literal.

    `throw new IllegalArgumentException("bad " + name)` has no fixed text, and
    taking the first literal would quote half a sentence. A message quoted
    wrongly is worse than one omitted, because a reader searches for the words
    this document gave them -- so a literal counts only when a `)` closes the
    call directly behind it. A text block is skipped for the same reason: the
    tokenizer keeps its delimiters, and its text is a paragraph rather than a
    message.
    """

    if cursor + 2 >= len(tokens):
        return None
    opening, literal, closing = tokens[cursor], tokens[cursor + 1], tokens[cursor + 2]
    if opening.kind != "punctuation" or opening.value != "(":
        return None
    if literal.kind != "string" or literal.value.startswith('"""'):
        return None
    if closing.kind != "punctuation" or closing.value != ")":
        return None
    return literal.value.strip() or None


def throw_sites(tokens: list[Token]) -> list[tuple[str | None, str | None, int]]:
    """`(exception type, literal message, line)` for every `throw` in a file.

    `throw` is reserved, and comments and string bodies never reach the token
    stream, so a match here is a throw rather than the word inside a sentence.

    `throw ex` re-raises something caught elsewhere and names no type of its
    own. It is counted where it appears and left unnamed, the way the Python
    reader treats a bare `raise`: the file does fail there, and saying which
    type would mean guessing.
    """

    found: list[tuple[str | None, str | None, int]] = []
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "throw":
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if following is None or following.kind != "identifier" or following.value != "new":
            found.append((None, None, token.line))
            continue
        name, cursor = _dotted_name(tokens, index + 2)
        if not name:
            found.append((None, None, token.line))
            continue
        found.append((_simple_name(name), _literal_argument(tokens, cursor), token.line))
    return found


def declared_throws(tokens: list[Token]) -> dict[str, dict[str, int]]:
    """Exception types named in `throws` clauses, by count and first line.

    Java is the only language this engine reads whose signatures declare what
    a call may fail with and whose compiler holds callers to it, so this is a
    surface the other readers have nothing to match. It is also the only way
    to see the failure of a file that throws nothing itself: `BlockingQueue`
    declares `InterruptedException` on six methods and contains no `throw`.

    Javadoc states the same thing with `@throws`, and states it more often --
    in `java.util.concurrent`, three times as often. The tokenizer drops
    comments, so what is counted here is the compiler's copy rather than the
    prose beside it, which is the copy that is checked.
    """

    found: dict[str, dict[str, int]] = {}
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value != "throws":
            continue
        cursor = index + 1
        while cursor < len(tokens):
            name, cursor = _dotted_name(tokens, cursor)
            if not name:
                break
            entry = found.setdefault(_simple_name(name), {"count": 0, "line": token.line})
            entry["count"] += 1
            if (
                cursor < len(tokens)
                and tokens[cursor].kind == "punctuation"
                and tokens[cursor].value == ","
            ):
                cursor += 1
                continue
            break
    return found


def _quote(text: str) -> str:
    folded = " ".join(text.split())
    if len(folded) <= MAX_MESSAGE_CHARS:
        return f'"{folded}"'
    return f'"{folded[: MAX_MESSAGE_CHARS - 1]}\u2026"'


def _is_public(member: JavaMember, owner_kind: str) -> bool:
    """Whether a member is part of its owner's public surface.

    Modifiers are not the whole answer. Members of an interface and of an
    annotation type are implicitly public -- `String greet(String n);` carries
    no modifier and is still callable by anyone -- so counting explicit
    `public` reported every interface in a codebase as exposing nothing, which
    is the opposite of what an interface is for.

    Java 9 allows explicitly private interface methods, so the exception is
    written as "public unless declared otherwise" rather than "always public".
    """

    if owner_kind in {"interface", "annotation_type"}:
        return not {"private", "protected"} & set(member.modifiers)
    return "public" in member.modifiers


class JavaLexicalAnalyzer:
    """Read Java declarations into claims, each pinned to a line."""

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
        analyzed_files = 0
        test_receipts: list[str] = []

        def receipt(path: str, line: int, kind: str, symbol: str | None, excerpt: str) -> str:
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
            return record.evidence_id

        for file_record in eligible:
            source_path = snapshot.root / Path(file_record.path)
            try:
                payload = source_path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != file_record.sha256:
                    raise ValueError("content changed after snapshot")
                source = payload.decode("utf-8", errors="strict")
                tokens = tokenize(source)
                types = declared_types(tokens)
                members = declared_members(tokens)
                constants = enum_constants(tokens)
                components = record_components(tokens)
                # A constant carrying constructor arguments has the shape
                # of a method declaration -- `RED("r")` -- and the member
                # reader classifies it as one. It is a constant, counted
                # as such above, so drop the impostor rather than report
                # an enum with a method named after each of its values.
                named_constants = {(owner, name) for owner, name, _ in constants}
                members = [
                    member
                    for member in members
                    if (member.owner, member.name) not in named_constants
                ]
                package = package_name(tokens)
                imports = imported_types(tokens)
                file_enums = declared_enums(tokens)
                file_shapes = declared_shapes(tokens)
                file_constants = declared_constants(tokens)
                file_strings = {
                    name: entry
                    for name, entry in file_constants.items()
                    if "value" in entry and not declares_a_number(entry["value"])
                }
                file_constants = {
                    name: entry
                    for name, entry in file_constants.items()
                    if name not in file_strings
                }
            except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
                failures.append(f"{file_record.path}: {exc.__class__.__name__}: {exc}")
                continue

            path = file_record.path
            lines = source.splitlines()

            def line_text(number: int, source_lines: list[str] = lines) -> str:
                return source_lines[number - 1] if 0 < number <= len(source_lines) else ""

            def qualify(name: str, owner: str = package) -> str:
                return f"{owner}.{name}" if owner else name

            # One symbol per file to carry what the file declares. Every other
            # reader has one and this did not, so a Java package's vocabularies
            # and constants had nowhere to live even once they were read.
            if file_enums or file_constants or file_strings or file_shapes:
                symbols.append(
                    SymbolRecord(
                        symbol_id=stable_id(
                            "symbol", (snapshot.snapshot_id, path, "compilation-unit")
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        path=path,
                        qualified_name=qualify(Path(path).stem),
                        kind="module",
                        start_line=1,
                        end_line=max(1, file_record.line_count),
                        language="Java",
                        analyzer=ANALYZER_VERSION,
                        metadata={
                            "analysis_level": "lexical",
                            **({"tunables": file_constants} if file_constants else {}),
                            **({"collection_constants": file_enums} if file_enums else {}),
                            **({"string_constants": file_strings} if file_strings else {}),
                            **({"model_fields": file_shapes} if file_shapes else {}),
                        },
                    )
                )

            for item in types:
                qualified = qualify(item.name)
                symbols.append(
                    SymbolRecord(
                        symbol_id=stable_id(
                            # The line is part of the identity because a name
                            # can legitimately repeat in one file: `ReduceOps`
                            # declares a local class called `ReducingSink`
                            # twelve times, once inside each method. Keying on
                            # the name alone collapsed all twelve into a single
                            # row and lost twenty-four symbols across
                            # `java.base` without reporting anything.
                            "symbol",
                            (snapshot.snapshot_id, path, qualified, item.line, ANALYZER_VERSION),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        path=path,
                        qualified_name=qualified,
                        kind=item.kind,
                        start_line=item.line,
                        end_line=item.line,
                        language="Java",
                        analyzer=ANALYZER_VERSION,
                        metadata={"local": item.local, "modifiers": list(item.modifiers)},
                    )
                )
                # A local class cannot be named from outside the method that
                # declares it, so it joins no surface and satisfies no
                # contract any caller can rely on.
                if item.local:
                    continue
                for supertype in item.supertypes:
                    supporting = receipt(
                        path, item.line, "java_supertype", qualified, line_text(item.line)
                    )
                    edges.append(
                        EdgeRecord(
                            edge_id=stable_id(
                                "edge",
                                (snapshot.snapshot_id, path, qualified, "implements", supertype),
                            ),
                            snapshot_id=snapshot.snapshot_id,
                            source_symbol_id=None,
                            source_path=path,
                            relationship="implements",
                            target_ref=supertype,
                            target_symbol_id=None,
                            evidence_id=supporting,
                            analyzer=ANALYZER_VERSION,
                        )
                    )
                    if describes_the_product(file_record.role):
                        claims.append(
                            self._claim(
                                snapshot,
                                created_at,
                                text=(
                                    f"{qualified} declares {supertype} as a supertype, so it "
                                    "satisfies that contract wherever the supertype is expected."
                                ),
                                category="trait_implementation",
                                supporting=(supporting,),
                                path=path,
                            )
                        )
                if (
                    describes_the_product(file_record.role)
                    and "public" in item.modifiers
                    and item.kind in PUBLIC_KINDS
                ):
                    simple = item.name.rsplit(".", 1)[-1]
                    # An enum's constants and a record's components are its
                    # public surface, and both are declared outside the member
                    # body: constants before the first `;`, components in the
                    # header. Counting only body members reported every enum
                    # and record as exposing whatever else it happened to
                    # declare, which for most of them is nothing at all.
                    implicit = sum(1 for owner, _, _ in constants if owner == simple)
                    implicit += sum(1 for owner, _, _, _ in components if owner == simple)
                    exposed = [
                        member
                        for member in members
                        if member.owner == simple and _is_public(member, item.kind)
                    ]
                    supporting = receipt(
                        path, item.line, "java_public_type", qualified, line_text(item.line)
                    )
                    claims.append(
                        self._claim(
                            snapshot,
                            created_at,
                            text=(
                                f"{qualified} is a public {item.kind} exposing "
                                f"{len(exposed) + implicit} public member(s). Renaming or "
                                "removing one is a breaking change for every caller."
                            ),
                            category="public_api",
                            supporting=(supporting,),
                            importance="high",
                            path=path,
                        )
                    )

            for member in members:
                owner = qualify(member.owner)
                if (
                    member.kind == "method"
                    and member.name == "main"
                    and {"public", "static"} <= set(member.modifiers)
                ):
                    supporting = receipt(
                        path, member.line, "java_entry", owner, line_text(member.line)
                    )
                    if describes_the_product(file_record.role):
                        claims.append(
                            self._claim(
                                snapshot,
                                created_at,
                                text=f"{owner}.main is a program entry point.",
                                category="application_entry",
                                supporting=(supporting,),
                                importance="high",
                                path=path,
                            )
                        )
                if TEST_ANNOTATIONS & set(member.annotations):
                    test_receipts.append(
                        receipt(path, member.line, "java_test", owner, line_text(member.line))
                    )
                if (
                    member.kind == "field"
                    and "static" in member.modifiers
                    and "final" not in member.modifiers
                ):
                    supporting = receipt(
                        path, member.line, "java_static_state", owner, line_text(member.line)
                    )
                    claims.append(
                        self._claim(
                            snapshot,
                            created_at,
                            text=(
                                f"{owner}.{member.name} is a non-final static field, so its "
                                "value changes while the process runs and every caller in "
                                "that process shares it; a second instance of this program "
                                "observes none of those changes."
                            ),
                            category="process_local_state",
                            supporting=(supporting,),
                            path=path,
                        )
                    )
                for annotation, argument in member.annotation_arguments:
                    if annotation not in ROUTE_ANNOTATIONS or not argument.startswith("/"):
                        continue
                    verb = ROUTE_ANNOTATIONS[annotation]
                    supporting = receipt(
                        path, member.line, "java_route", owner, line_text(member.line)
                    )
                    # An annotation that names no verb is reported without
                    # one. Guessing GET would be a statement about this
                    # reader rather than about the code.
                    prefix = f"{verb} " if verb else ""
                    qualifier = (
                        ""
                        if verb
                        else (
                            " The annotation names no HTTP method, so which methods reach "
                            "this handler is decided by the framework rather than declared "
                            "here."
                        )
                    )
                    claims.append(
                        self._claim(
                            snapshot,
                            created_at,
                            text=(
                                f"{prefix}{argument} is handled by "
                                f"{owner}.{member.name}.{qualifier}"
                            ),
                            category="http_route",
                            supporting=(supporting,),
                            importance="high",
                            path=path,
                        )
                    )

            throws = throw_sites(tokens)
            declared = declared_throws(tokens)
            if (throws or declared) and describes_the_product(file_record.role):
                kinds = sorted({name for name, _, _ in throws if name})
                messages = [message for _, message, _ in throws if message]
                first = min(
                    [line for _, _, line in throws]
                    + [int(entry["line"]) for entry in declared.values()]
                )
                sentences: list[str] = []
                if throws:
                    detail = (
                        f": {', '.join(f'`{item}`' for item in kinds[:MAX_NAMED])}" if kinds else ""
                    )
                    quoted = (
                        " Including " + ", ".join(_quote(item) for item in messages[:3]) + "."
                        if messages
                        else ""
                    )
                    sentences.append(
                        f"{path} throws in {len(throws):,} place(s), of "
                        f"{len(kinds):,} distinct type(s){detail}. A caller that does not "
                        f"catch them sees them propagate.{quoted}"
                    )
                if declared:
                    named = ", ".join(f"`{item}`" for item in sorted(declared)[:MAX_NAMED])
                    sentences.append(
                        f"{path} declares {len(declared):,} exception type(s) in `throws` "
                        f"clauses: {named}. Where the declared type is a checked one, the "
                        "compiler requires every caller to catch it or redeclare it."
                    )
                claims.append(
                    self._claim(
                        snapshot,
                        created_at,
                        text=" ".join(sentences),
                        category="failure_surface",
                        supporting=(
                            receipt(path, first, "failure_surface", None, line_text(first)),
                        ),
                        path=path,
                    )
                )

            if describes_the_product(file_record.role):
                for name, kind, line in dict.fromkeys(environment_reads(tokens)):
                    claims.append(
                        self._claim(
                            snapshot,
                            created_at,
                            text=(
                                f"{path} reads {kind} {name} at run time. It is supplied by "
                                + (
                                    "whatever starts the process"
                                    if kind == "environment setting"
                                    else "`-D` on the command line or by the program itself"
                                )
                                + ", so a machine that does not set it runs a different "
                                "program from the one this file describes."
                            ),
                            category="configuration_read",
                            supporting=(
                                receipt(path, line, "environment_read", None, line_text(line)),
                            ),
                            path=path,
                        )
                    )

            for target, line in imports:
                edges.append(
                    EdgeRecord(
                        edge_id=stable_id(
                            "edge", (snapshot.snapshot_id, path, "imports", target, line)
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        source_symbol_id=None,
                        source_path=path,
                        relationship="imports",
                        target_ref=target,
                        target_symbol_id=None,
                        evidence_id=None,
                        analyzer=ANALYZER_VERSION,
                    )
                )
            analyzed_files += 1

        if test_receipts:
            claims.append(
                self._claim(
                    snapshot,
                    created_at,
                    text=f"Java source declares {len(test_receipts)} annotated test method(s).",
                    category="testing",
                    supporting=tuple(test_receipts[:24]),
                )
            )

        coverage = CoverageRecord(
            analyzer=ANALYZER_VERSION,
            language="Java",
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
            invalidation_keys=(f"file:{path}",) if path else ("language:java",),
            alternative_hypotheses=(
                (
                    "Declarations are read lexically rather than compiled, so a recorded "
                    "annotation is the one written at the site rather than the one a "
                    "framework resolves through inheritance and meta-annotation."
                ),
            ),
        )
