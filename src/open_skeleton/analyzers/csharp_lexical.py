# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""What a C# file declares: its namespace, its public surface, its failures.

Sixteen C# files sat in this corpus and fifteen of them were read by nothing.
C# is not a marginal language -- it is most of .NET and all of Unity scripting
-- and a repository written in it reported no public API at all, which is a
statement about this engine rather than about the code.

The facts recovered are the ones every other language reader here already
produces, under the same category names, so a reader asks one question rather
than one per language. `using` directives are imports. A `public` type or
member is public surface: in C# that word is load-bearing in a way it is not
in Python, because removing it is a compile error for every caller. A `throw`
is what comes out of a call that is not a value.

Lexical, and says so. C#'s grammar is large -- generics, tuples, pattern
matching, local functions, expression-bodied everything -- and the parts read
here do not need it. What they do need is to not be fooled by the file's own
text, so comments and string bodies are blanked first: `// public class Foo`
declares nothing and "throw" inside a message is not a throw. Verbatim
(`@"..."`) and interpolated (`$"..."`) strings are handled, since both appear
throughout real Unity code.

Two limits worth stating. Nesting is not tracked, so a `public` member of a
private nested class is still reported as public surface; that over-reports
rather than invents, and the member does exist. And a constructor is not
counted as a member, because `public Creature(` has no return type to
distinguish it from a method call at this level of reading.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

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

ANALYZER_NAME = "csharp-lexical"
ANALYZER_VERSION = "csharp-lexical/v1"

MAX_SOURCE_BYTES = 1_000_000
MAX_NAMED = 12
MAX_MESSAGE_CHARS = 60

NAMESPACE = re.compile(r"\bnamespace\s+([\w.]+)")
USING = re.compile(r"(?m)^\s*using\s+(?:static\s+)?([\w.]+)\s*;")
TYPE_KEYWORDS = "class|struct|interface|enum|record"
DECLARED_TYPE = re.compile(
    r"\b(?P<access>public|internal|protected|private)\s+"
    r"(?:(?:sealed|static|abstract|partial|readonly|ref|unsafe)\s+)*"
    rf"(?P<kind>{TYPE_KEYWORDS})\s+(?P<name>\w+)"
)
# A member is `public`, then modifiers, then a return type, then a name. The
# lookahead keeps type declarations out: `public sealed class Creature` would
# otherwise read as a member named `Creature` of type `class`.
PUBLIC_MEMBER = re.compile(
    r"\bpublic\s+"
    r"(?:(?:static|readonly|virtual|override|async|sealed|abstract|const|new|extern|unsafe|partial|event|required)\s+)*"
    rf"(?!(?:{TYPE_KEYWORDS})\b)"
    r"(?P<type>[\w.<>\[\],?]+)\s+(?P<name>\w+)\s*(?P<tail>[({=;])"
)
THROW_NEW = re.compile(r"\bthrow\s+new\s+(?P<type>[\w.]+)\s*\((?P<rest>[^)]{0,200})")
THROW_ANY = re.compile(r"(?<![\w.])throw\b")


def blank_noise(source: str) -> str:
    """Replace comments and string bodies with spaces, keeping every newline.

    Blanking rather than deleting preserves every offset, so a line computed
    on the result is the line in the file a reader opens.
    """

    out = list(source)
    index = 0
    length = len(source)

    def blank(start: int, end: int) -> None:
        for position in range(start, min(end, length)):
            if out[position] != "\n":
                out[position] = " "

    while index < length:
        if source.startswith("//", index):
            end = source.find("\n", index)
            end = length if end < 0 else end
            blank(index, end)
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = length if end < 0 else end + 2
            blank(index, end)
            index = end
            continue
        if source.startswith(('@"', '$@"', '@$"'), index):
            # A verbatim string ends at a lone `"`; a doubled `""` is an escape.
            opening = source.find('"', index)
            end = opening + 1
            while end < length:
                if source[end] == '"':
                    if end + 1 < length and source[end + 1] == '"':
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            blank(index, end)
            index = end
            continue
        if source[index] in "\"'" or source.startswith('$"', index):
            start = index
            quote = '"' if source.startswith('$"', index) else source[index]
            end = (index + 2) if source.startswith('$"', index) else (index + 1)
            while end < length:
                if source[end] == "\\":
                    end += 2
                    continue
                if source[end] == quote:
                    end += 1
                    break
                if source[end] == "\n":
                    break
                end += 1
            blank(start, end)
            index = end
            continue
        index += 1
    return "".join(out)


def _line_of(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def declared_namespace(clean: str) -> str | None:
    match = NAMESPACE.search(clean)
    return match.group(1) if match else None


def imported_namespaces(clean: str) -> dict[str, int]:
    """`using` directives, with the line each appears on."""

    found: dict[str, int] = {}
    for match in USING.finditer(clean):
        found.setdefault(match.group(1), _line_of(clean, match.start(1)))
    return found


def declared_types(clean: str) -> list[tuple[str, str, str, int]]:
    """`(access, kind, name, line)` for every declared type."""

    found: list[tuple[str, str, str, int]] = []
    seen: set[str] = set()
    for match in DECLARED_TYPE.finditer(clean):
        name = match.group("name")
        if name in seen:
            continue
        seen.add(name)
        found.append(
            (
                match.group("access"),
                match.group("kind"),
                name,
                _line_of(clean, match.start("name")),
            )
        )
    return found


# The value is captured with its surrounding space rather than trimmed by the
# pattern. Blanking replaces a string body *and its quotes* with spaces, so a
# trimming `\s*` consumed the whole value on the blanked text and left the
# group holding one character -- which read back from the source as a lone
# quote instead of `checkout`.
CONSTANT = re.compile(r"\b(?:const|static\s+readonly)\s+[\w.<>\[\],\s]+?\s(\w+)\s*=([^;]*);")
ENUM = re.compile(r"\benum\s+(\w+)\s*(?::\s*[\w.]+\s*)?\{([^}]*)\}")
MEMBER = re.compile(r"[A-Za-z_]\w*")
MAX_ENUM_MEMBERS = 64


def declared_constants(source: str, clean: str) -> dict[str, dict[str, object]]:
    """`const` and `static readonly` fields holding a literal, with the value.

    Every other language this engine reads records these and C# recorded
    nothing at all -- no numbers, no strings, no vocabularies -- which a
    reader-parity check found in one run after four fixtures had each found
    the same gap one language at a time.

    Matching runs against the blanked text so a constant mentioned in a
    comment is not read as one, and the value is taken from the original at
    the same offsets, because blanking removes the body of a string and the
    body is exactly what a reader wants.
    """

    found: dict[str, dict[str, object]] = {}
    for match in CONSTANT.finditer(clean):
        # A match landing on blanked text came from a comment or a string.
        if clean[match.start()].isspace():
            continue
        raw = source[match.start(2) : match.end(2)].strip()
        if not raw:
            continue
        value = raw
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            value = raw[1:-1]
        elif not raw.replace("_", "").lstrip("-").replace(".", "", 1).isdigit():
            # Anything that is neither a quoted string nor a plain number is
            # computed, and naming its first token would state a value the
            # program never holds.
            continue
        found[match.group(1)] = {"value": value, "line": _line_of(clean, match.start())}
    return found


def declared_enums(clean: str) -> dict[str, dict[str, object]]:
    """Enum members, which are how C# declares a closed set of values."""

    found: dict[str, dict[str, object]] = {}
    for match in ENUM.finditer(clean):
        if clean[match.start()].isspace():
            continue
        members: list[str] = []
        for entry in match.group(2).split(","):
            # `Get = 1` names one member; the assigned value is its ordinal.
            name = entry.split("=", 1)[0].strip()
            found_name = MEMBER.match(name)
            if found_name:
                members.append(found_name.group(0))
        if 2 <= len(members) <= MAX_ENUM_MEMBERS:
            found[match.group(1)] = {
                "members": members,
                "line": _line_of(clean, match.start()),
            }
    return found


def public_members(clean: str) -> list[tuple[str, str, int]]:
    """`(kind, name, line)` for every public method, property, or field."""

    found: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for match in PUBLIC_MEMBER.finditer(clean):
        name = match.group("name")
        if name in seen:
            continue
        seen.add(name)
        kind = "method" if match.group("tail") == "(" else "property"
        found.append((kind, name, _line_of(clean, match.start("name"))))
    return found


# The pattern stops at the paren. It cannot ask the blanked copy whether a
# quote follows, because blanking replaces the whole literal, quotes included.
ENVIRONMENT_READ = re.compile(r"\bEnvironment\s*\.\s*GetEnvironmentVariable\s*\(")


def environment_reads(source: str, clean: str) -> list[tuple[str, int]]:
    """Settings a file reads from its environment, as `(name, line)`.

    The call is found on the blanked copy, so one written inside a comment or
    a string is not a read. The name is then taken from the original at the
    same offset, because blanking empties the argument -- reading it from the
    blanked copy would report a program that reads a setting called nothing.

    A name built at run time yields no literal and is not recorded: its value
    is not knowable without running the program.
    """

    found: list[tuple[str, int]] = []
    for match in ENVIRONMENT_READ.finditer(clean):
        cursor = match.end()
        while cursor < len(source) and source[cursor] in " \t":
            cursor += 1
        while cursor < len(source) and source[cursor] in "@$":
            cursor += 1
        if cursor >= len(source) or source[cursor] != '"':
            continue
        closing = source.find('"', cursor + 1)
        if closing < 0:
            continue
        name = source[cursor + 1 : closing].strip()
        if name:
            found.append((name, _line_of(clean, match.start())))
    return found


def throw_sites(source: str, clean: str) -> list[tuple[str | None, str | None, int]]:
    """`(exception type, literal message, line)` for every throw.

    Only a plain string literal is quoted. An interpolated `$"..."` has no
    fixed text, and a message quoted wrongly is worse than one omitted because
    a reader will search for the words this document gave them.
    """

    found: list[tuple[str | None, str | None, int]] = []
    for match in THROW_ANY.finditer(clean):
        line = _line_of(clean, match.start())
        typed = THROW_NEW.match(clean, match.start())
        if typed is None:
            found.append((None, None, line))
            continue
        # The argument text is read from the original, since the cleaned copy
        # has had its string bodies blanked.
        raw = source[typed.start("rest") : typed.end("rest")]
        message: str | None = None
        stripped = raw.strip()
        if stripped.startswith('"'):
            closing = stripped.find('"', 1)
            if closing > 0:
                candidate = stripped[1:closing].strip()
                message = candidate or None
        found.append((typed.group("type"), message, line))
    return found


def _excerpt(lines: list[str], line: int, fallback: str) -> str:
    """The source line a receipt hashes, or the path when the line is gone."""

    return lines[line - 1] if 0 < line <= len(lines) else fallback


def _quote(text: str) -> str:
    folded = " ".join(text.split())
    if len(folded) <= MAX_MESSAGE_CHARS:
        return f'"{folded}"'
    return f'"{folded[: MAX_MESSAGE_CHARS - 1]}…"'


class CSharpLexicalAnalyzer:
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

        eligible = [
            item
            for item in snapshot.files
            if item.language == "C#" and item.size_bytes <= MAX_SOURCE_BYTES
        ]
        analyzed = 0

        def receipt(path: str, line: int, kind: str, excerpt: str) -> EvidenceRecord:
            record = EvidenceRecord(
                evidence_id=stable_id(
                    "evidence", (snapshot.snapshot_id, path, line, kind, ANALYZER_VERSION)
                ),
                snapshot_id=snapshot.snapshot_id,
                path=path,
                start_line=line,
                end_line=line,
                symbol=None,
                evidence_kind=kind,
                excerpt_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                analyzer=ANALYZER_VERSION,
                created_at=created_at,
            )
            evidence.append(record)
            return record

        def claim(text: str, category: str, importance: str, supporting: str, path: str) -> None:
            claims.append(
                ClaimRecord(
                    claim_id=stable_id(
                        "claim", (snapshot.snapshot_id, category, text, ANALYZER_VERSION)
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    claim=text,
                    category=category,
                    status="verified",
                    confidence=1.0,
                    importance=importance,
                    produced_by=ANALYZER_VERSION,
                    created_at=created_at,
                    verified_at=created_at,
                    supporting_evidence=(supporting,),
                    invalidation_keys=(f"file:{path}",),
                )
            )

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

            analyzed += 1
            clean = blank_noise(source)
            lines = source.splitlines()
            namespace = declared_namespace(clean)
            imports = imported_namespaces(clean)
            types = declared_types(clean)
            members = public_members(clean)
            throws = throw_sites(source, clean)
            settings = environment_reads(source, clean)

            module = namespace or Path(file_record.path).stem
            names: dict[str, int] = {}
            for _, _, name, line in types:
                names.setdefault(name, line)
            for _, name, line in members:
                names.setdefault(name, line)
            for name, line in imports.items():
                names.setdefault(name, line)

            file_values = declared_constants(source, clean)
            file_numbers = {
                name: entry
                for name, entry in file_values.items()
                if declares_a_number(entry.get("value"))
            }
            file_strings = {
                name: entry
                for name, entry in file_values.items()
                if "value" in entry and not declares_a_number(entry["value"])
            }
            file_enums = declared_enums(clean)
            module_symbol = stable_id(
                "symbol", (snapshot.snapshot_id, file_record.path, "module", ANALYZER_VERSION)
            )
            symbols.append(
                SymbolRecord(
                    symbol_id=module_symbol,
                    snapshot_id=snapshot.snapshot_id,
                    path=file_record.path,
                    qualified_name=module,
                    kind="module",
                    start_line=1,
                    end_line=max(1, file_record.line_count),
                    language="C#",
                    analyzer=ANALYZER_VERSION,
                    metadata={
                        "analysis_level": "lexical",
                        **({"name_index": names} if names else {}),
                        **({"tunables": file_numbers} if file_numbers else {}),
                        **({"string_constants": file_strings} if file_strings else {}),
                        **({"collection_constants": file_enums} if file_enums else {}),
                    },
                )
            )
            for access, kind, name, line in types:
                symbols.append(
                    SymbolRecord(
                        symbol_id=stable_id(
                            "symbol",
                            (snapshot.snapshot_id, file_record.path, name, ANALYZER_VERSION),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        path=file_record.path,
                        qualified_name=f"{module}.{name}" if namespace else name,
                        kind=kind,
                        start_line=line,
                        end_line=line,
                        language="C#",
                        analyzer=ANALYZER_VERSION,
                        metadata={"access": access},
                    )
                )
            for target, line in imports.items():
                edges.append(
                    EdgeRecord(
                        edge_id=stable_id(
                            "edge",
                            (
                                snapshot.snapshot_id,
                                module_symbol,
                                "imports",
                                target,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        source_symbol_id=module_symbol,
                        source_path=file_record.path,
                        relationship="imports",
                        target_ref=target,
                        target_symbol_id=None,
                        evidence_id=receipt(
                            file_record.path,
                            line,
                            "import",
                            _excerpt(lines, line, file_record.path),
                        ).evidence_id,
                        analyzer=ANALYZER_VERSION,
                    )
                )

            if not describes_the_product(file_record.role):
                continue

            public_types = [item for item in types if item[0] == "public"]
            if public_types or members:
                first = min(
                    [line for _, _, _, line in public_types] + [line for _, _, line in members]
                )
                named = ", ".join(f"`{name}`" for _, _, name, _ in public_types[:MAX_NAMED])
                surface = (
                    f"{len(public_types):,} public type(s)"
                    + (f": {named}" if named else "")
                    + f" and {len(members):,} public member(s)"
                )
                claim(
                    (
                        f"{file_record.path} declares {surface}. In C# `public` is what a "
                        "caller outside the assembly can reach, so removing one is a "
                        "compile error for everyone who used it."
                    ),
                    "public_api",
                    "high",
                    receipt(
                        file_record.path,
                        first,
                        "public_api",
                        _excerpt(lines, first, file_record.path),
                    ).evidence_id,
                    file_record.path,
                )

            for name, line in dict.fromkeys(settings):
                claim(
                    (
                        f"{file_record.path} reads environment setting {name} at run time. "
                        "A machine that does not set it runs a different program from the "
                        "one this file describes."
                    ),
                    "configuration_read",
                    "medium",
                    receipt(
                        file_record.path,
                        line,
                        "environment_read",
                        _excerpt(lines, line, file_record.path),
                    ).evidence_id,
                    file_record.path,
                )

            if throws:
                first = throws[0][2]
                kinds = sorted({item[0] for item in throws if item[0]})
                messages = [item[1] for item in throws if item[1]]
                detail = (
                    f": {', '.join(f'`{item}`' for item in kinds[:MAX_NAMED])}" if kinds else ""
                )
                quoted = (
                    " Including " + ", ".join(_quote(item) for item in messages[:3]) + "."
                    if messages
                    else ""
                )
                claim(
                    (
                        f"{file_record.path} throws in {len(throws):,} place(s), of "
                        f"{len(kinds):,} distinct type(s){detail}. A caller that does not "
                        f"catch them sees them propagate.{quoted}"
                    ),
                    "failure_surface",
                    "medium",
                    receipt(
                        file_record.path,
                        first,
                        "failure_surface",
                        _excerpt(lines, first, file_record.path),
                    ).evidence_id,
                    file_record.path,
                )

        coverage = CoverageRecord(
            analyzer=ANALYZER_VERSION,
            language="C#",
            eligible_files=len(eligible),
            analyzed_files=analyzed,
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
