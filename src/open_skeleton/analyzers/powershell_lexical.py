# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""What a PowerShell script declares: its parameters, functions, and failures.

Twenty-one PowerShell files sat in this corpus and no reader touched one of
them. They were `Unknown` to every analyzer, so a repository whose install
step, release process and gate are written in PowerShell reported that it had
no entry points and no public surface -- a statement about this engine rather
than about the repository.

The three facts worth recovering are the ones every other language analyzer
here already reports. A script's `param()` block is its command line, exactly
as `add_argument` is Python's and `#[arg(long)]` is Rust's, so it produces the
same `command_line_interface` claim rather than a new category. A `throw` is
what can come out of a call that is not a value.

Functions are public surface only where the file publishes them -- a `.psm1`,
or anything calling `Export-ModuleMember`. A `.ps1` is run rather than
imported, and its functions are the steps it runs. Every function is indexed
and searchable either way; what changes is whether the document calls it a
contract.

This is lexical, and says so. PowerShell's grammar is large -- a full parse
would have to handle expandable strings, subexpressions, splatting and
attribute arguments -- and the parts read here do not need one. What it does
need is to not be fooled by its own input, so comments and string bodies are
blanked before anything is matched: `# function Get-Thing` is a comment and
"throw" inside a message is not a throw. Blanking preserves newlines, so every
line number reported is the line in the file a reader will open.

What it cannot do is bounded and stated. A function defined inside a string
and invoked with `Invoke-Expression` is invisible here, and so is a parameter
name built at run time. Both are absent rather than guessed.
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

ANALYZER_NAME = "powershell-lexical"
ANALYZER_VERSION = "powershell-lexical/v1"

MAX_SOURCE_BYTES = 1_000_000
MAX_NAMED = 12
MAX_MESSAGE_CHARS = 60

FUNCTION = re.compile(r"(?im)^[ \t]*function[ \t]+([A-Za-z_][\w-]*)")
PARAM_BLOCK = re.compile(r"(?i)\bparam[ \t\r\n]*\(")
# A parameter is a variable at the top level of the block. The type and any
# attributes sit before it and are not part of the name a caller types.
VARIABLE = re.compile(r"\$([A-Za-z_]\w*)")
# A throw is a statement anywhere, not only at the start of a line: guard
# clauses are routinely written `if (-not $Name) { throw '...' }`, and an
# anchored pattern missed every one of them. The lookbehind keeps `$throw`
# and `-throw` from matching, since neither is the keyword.
THROW = re.compile(r"(?i)(?<![\w$-])throw\b")


@dataclass(frozen=True, slots=True)
class ParameterBlock:
    """One `param(...)`, and whether it belongs to the script or a function."""

    line: int
    names: tuple[str, ...]
    script_level: bool


def _blank_noise(source: str, *, keep_expandable: bool = False) -> str:
    r"""Replace comments and string bodies with spaces, keeping every newline.

    `keep_expandable` leaves double-quoted strings and `@" ... "@` blocks
    intact while still blanking comments and single-quoted text. PowerShell
    expands a double-quoted string, so `"$env:windir\system32"` is a real read
    of a real setting; blanking it erased half the environment reads in the
    modules Windows ships. The string is still walked past either way, so a
    `#` inside one is not mistaken for a comment.

    A reader that matches on raw text finds `function` in a comment and
    `throw` inside a message. Blanking rather than deleting keeps every
    offset, so a line number computed on the result is the line in the file.

    PowerShell's here-strings (`@" ... "@`) are handled because a script that
    embeds a block of text is exactly the script most likely to contain words
    this reader is looking for.
    """

    out = list(source)
    index = 0
    length = len(source)

    def blank(start: int, end: int) -> None:
        for position in range(start, min(end, length)):
            if out[position] != "\n":
                out[position] = " "

    while index < length:
        char = source[index]
        if source.startswith("<#", index):
            end = source.find("#>", index + 2)
            end = length if end < 0 else end + 2
            blank(index, end)
            index = end
            continue
        if char == "#":
            end = source.find("\n", index)
            end = length if end < 0 else end
            blank(index, end)
            index = end
            continue
        if source.startswith(('@"', "@'"), index):
            terminator = '"@' if source[index + 1] == '"' else "'@"
            end = source.find(terminator, index + 2)
            end = length if end < 0 else end + 2
            if not (keep_expandable and source[index + 1] == '"'):
                blank(index, end)
            index = end
            continue
        if char in "\"'":
            end = index + 1
            while end < length:
                if source[end] == "`" and char == '"':
                    end += 2
                    continue
                if source[end] == char:
                    # A doubled quote is an escaped quote in PowerShell.
                    if end + 1 < length and source[end + 1] == char:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            if not (keep_expandable and char == '"'):
                blank(index, end)
            index = end
            continue
        index += 1
    return "".join(out)


def _line_of(source: str, position: int) -> int:
    return source.count("\n", 0, position) + 1


def _matching_paren(text: str, opener: int) -> int:
    """Index just past the `)` closing the `(` at ``opener``, or -1."""

    depth = 0
    for index in range(opener, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if not depth:
                return index + 1
    return -1


# The value is captured with its surrounding space rather than trimmed by the
# pattern. Blanking replaces a string body and its quotes with spaces, so a
# trimming `\s*` eats the whole value on the blanked text and leaves the group
# holding one character -- which reads back from the source as a lone quote.
# The C# reader hit this exact trap an hour earlier; the shape is the same
# wherever a reader matches on blanked text and reads values from the original.
SCRIPT_VARIABLE = re.compile(r"\$(?:script|global):(\w+)\s*=([^\r\n#]*)")
VALIDATE_SET = re.compile(r"\[ValidateSet\(([^)]*)\)\]", re.IGNORECASE)
SET_MEMBER = re.compile(r"""['"]([^'"]*)['"]""")
MAX_SET_MEMBERS = 64


def declared_values(source: str, clean: str) -> dict[str, dict[str, object]]:
    """Script-scoped variables holding a literal, with the value.

    Written against what PowerShell code actually does rather than what the
    language allows. A conformance snippet I wrote for this reader used
    `Set-Variable -Option Constant` and an `enum`, and the Microsoft module
    that ships with Windows contains zero of either: it declares its limits as
    `$script:MaxComponentDepth = 1024` and its vocabularies as `ValidateSet`.
    Building for the first pair would have satisfied my own test and read
    nothing real.

    Matching runs against the blanked text so a variable named in a comment is
    not read as one, and the value is taken from the original at the same
    offsets, because blanking removes a string's body.
    """

    found: dict[str, dict[str, object]] = {}
    for match in SCRIPT_VARIABLE.finditer(clean):
        if clean[match.start()].isspace():
            continue
        raw = source[match.start(2) : match.end(2)].strip().rstrip(";")
        if not raw:
            continue
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            value = raw[1:-1]
        elif raw.lstrip("-").replace(".", "", 1).isdigit():
            value = raw
        else:
            # `$script:Cache = @{}` and `$x = Get-Thing` are computed, and a
            # value this reader would have to run the shell to know is not one
            # it can state.
            continue
        found.setdefault(match.group(1), {"value": value, "line": _line_of(clean, match.start())})
    return found


def declared_value_sets(source: str, clean: str) -> dict[str, dict[str, object]]:
    """`ValidateSet` vocabularies, which is how PowerShell states a closed set.

    A parameter constrained to `Present` or `Absent` is a declared vocabulary
    in the same sense as a Rust enum or a Python frozenset, and it is the form
    every DSC resource in the shipped module uses.

    Named for the parameter the set constrains where that is visible, since a
    reader wants to know which knob the values belong to rather than that some
    set exists.
    """

    found: dict[str, dict[str, object]] = {}
    for match in VALIDATE_SET.finditer(clean):
        if clean[match.start()].isspace():
            continue
        body = source[match.start(1) : match.end(1)]
        members = [item for item in SET_MEMBER.findall(body) if item]
        if not 2 <= len(members) <= MAX_SET_MEMBERS:
            continue
        # The parameter this constrains is the next `$Name` in the source.
        tail = source[match.end() : match.end() + 200]
        parameter = re.search(r"\$(\w+)", tail)
        name = parameter.group(1) if parameter else f"ValidateSet@{_line_of(clean, match.start())}"
        found.setdefault(name, {"members": members, "line": _line_of(clean, match.start())})
    return found


def declared_functions(clean: str) -> dict[str, int]:
    """Function names a script defines, with the line each is defined on."""

    found: dict[str, int] = {}
    for match in FUNCTION.finditer(clean):
        name = match.group(1)
        line = _line_of(clean, match.start(1))
        found.setdefault(name, line)
    return found


def parameter_blocks(clean: str) -> list[ParameterBlock]:
    """Every `param(...)`, with the parameter names it declares.

    A block at brace depth zero is the script's own command line. One inside a
    function belongs to that function, and conflating the two would report a
    helper's arguments as flags a user can type.
    """

    blocks: list[ParameterBlock] = []
    for match in PARAM_BLOCK.finditer(clean):
        opener = clean.index("(", match.start())
        end = _matching_paren(clean, opener)
        if end < 0:
            continue
        body = clean[opener + 1 : end - 1]
        names = tuple(dict.fromkeys(VARIABLE.findall(body)))
        if not names:
            continue
        depth = clean.count("{", 0, match.start()) - clean.count("}", 0, match.start())
        blocks.append(
            ParameterBlock(
                line=_line_of(clean, match.start()),
                names=names,
                script_level=depth <= 0,
            )
        )
    return blocks


def throw_sites(source: str, clean: str) -> list[tuple[int, str | None]]:
    """Lines that throw, with the literal message where there is one.

    The message is read from the original text because the cleaned copy has
    had its strings blanked. Only a single-quoted string is quoted: PowerShell
    expands `$name` inside double quotes, so that text is a template and not
    what anybody will read in a console.
    """

    found: list[tuple[int, str | None]] = []
    for match in THROW.finditer(clean):
        line = _line_of(clean, match.start())
        newline = source.find("\n", match.end())
        tail = source[match.end() : len(source) if newline < 0 else newline]
        message: str | None = None
        stripped = tail.strip()
        if stripped.startswith("'"):
            closing = stripped.find("'", 1)
            if closing > 1:
                message = stripped[1:closing].strip() or None
        found.append((line, message))
    return found


IMPORT_MODULE = re.compile(r"(?i)(?<![\w-])Import-Module\b(?P<rest>[^\r\n]*)")
# A dot-source runs another file in this scope, which is what an import is.
# `.\build.ps1` invokes a script and is a different thing, so the space after
# the dot is load-bearing rather than formatting.
# The separator is inside the capture on purpose. `_blank_noise` turns
# `. "$here\Add-Numbers.ps1"` into a dot followed by a line of spaces, and
# a greedy `[ \t]+` outside the group then ate the blanked argument whole
# and reported no import at all. That is the same mistake as trimming a
# value on the blanked copy, which this reader and the C# one each made
# once already: the blanking decides where a match is, and the source
# decides what it says.
DOT_SOURCE = re.compile(r"(?m)^[ \t]*\.(?P<rest>[ \t][^\r\n]*)")
PATH_SEPARATORS = ("\\", "/")


def _import_target(rest: str) -> str | None:
    r"""The module or file an `Import-Module` or dot-source names, if it names one.

    Shipped PowerShell writes the argument every way the syntax allows, so
    each rejection below is a line that exists in Microsoft's own modules:
    `} | Import-Module -Force` names nothing and is piped its module, and
    `Import-Module -Name $provpackageapidll` names a variable whose value this
    reader would have to run the shell to learn. A name is recorded only when
    the file states one.

    `$PSScriptRoot\..\RunAsHelper.psm1` is kept as written -- it is the most
    common form in that corpus, and it does name one specific file, relative
    to a directory the shell fixes rather than computes.
    """

    parts = rest.strip().split()
    if not parts:
        return None
    if parts[0].lower() == "-name":
        parts = parts[1:]
    if not parts or parts[0].startswith("-"):
        return None
    candidate = parts[0].strip("'\"").rstrip(";)}").strip("'\"")
    if not candidate:
        return None
    # A bare `$module` is a name held in a variable. A path built on
    # `$PSScriptRoot` is not: the prefix is fixed and the rest is literal.
    if candidate.startswith("$") and not any(sep in candidate for sep in PATH_SEPARATORS):
        return None
    return candidate


def imported_modules(source: str, clean: str) -> list[tuple[str, int]]:
    """`(target, line)` for every module or file this one loads.

    Matching runs on the blanked copy, so `import-module` inside a sentence of
    documentation is not a load -- Microsoft's own `Pester` help text contains
    exactly that sentence. The argument is then read from the original at the
    same offsets, because blanking empties a quoted path.
    """

    found: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for pattern in (IMPORT_MODULE, DOT_SOURCE):
        for match in pattern.finditer(clean):
            rest = source[match.start("rest") : match.end("rest")]
            target = _import_target(rest)
            if target is None:
                continue
            if pattern is DOT_SOURCE and not any(
                target.lower().endswith(suffix) for suffix in (".ps1", ".psm1")
            ):
                # A dot followed by anything else is arithmetic, a range or a
                # method call at the start of a line. Only a script file is a
                # load, and a dot-source of one always names it.
                continue
            entry = (target, _line_of(clean, match.start()))
            if entry not in seen:
                seen.add(entry)
                found.append(entry)
    return sorted(found, key=lambda item: (item[1], item[0]))


# The trailing text is a lookahead so it is not consumed. Captured, it hid
# every second setting on a line: `"$env:SystemDrive\$env:HOMEPATH"` is two
# reads, and `finditer` resumes after the first match rather than inside it.
ENVIRONMENT = re.compile(r"(?i)\$\{?env:(?P<name>[A-Za-z_]\w*)\}?(?=(?P<rest>[^\r\n]{0,40}))")


def environment_reads(source: str) -> list[tuple[str, str, int]]:
    r"""Settings a script reads or sets, as `(name, direction, line)`.

    Matched against a copy that keeps expandable strings, because PowerShell
    expands them: `"$env:windir\system32"` reads `windir`, and blanking it
    erased 75 of the 148 `$env:` uses in the modules Windows ships. Comments
    and single-quoted text are still blanked, since neither is a read.

    A write is reported as a write. `$env:PSModulePath = ...` appears twelve
    times in that corpus and it changes what every child process inherits,
    which is the opposite of a setting this script needs supplied. `-eq` and
    `-ne` are comparisons rather than assignments, so only a bare `=` counts.
    """

    clean = _blank_noise(source, keep_expandable=True)
    found: list[tuple[str, str, int]] = []
    for match in ENVIRONMENT.finditer(clean):
        rest = match.group("rest").lstrip()
        direction = "sets" if rest.startswith("=") and not rest.startswith("==") else "reads"
        found.append((match.group("name"), direction, _line_of(clean, match.start())))
    return found


CLASS = re.compile(
    r"(?im)^[ \t]*class[ \t]+(?P<name>[A-Za-z_]\w*)[ \t]*(?::[ \t]*(?P<base>[A-Za-z_][\w.]*))?"
)
# `[uint64] $BytesFromPeers`, or the same thing over two lines -- one of the
# fifteen classes Windows ships writes every property with its type above its
# name, and requiring one line dropped that class whole.
#
# What keeps a parameter out is containment rather than these anchors: a
# property counts only when the brace it sits directly inside belongs to a
# class, and `param([String] $Method)` sits in a function's.
TYPED_PROPERTY = re.compile(
    r"(?m)^[ \t]*(?:\[[A-Za-z_][\w.]*\([^\r\n]*\)\][ \t]*)*"
    r"\[(?P<type>[A-Za-z_][\w.\[\]]*)\][ \t]*\r?\n?[ \t]*"
    r"\$(?P<name>[A-Za-z_]\w*)"
    # `\r?` before the anchor: a file with Windows line endings puts a carriage
    # return where `$` matches, and without this every property in every CRLF
    # file failed silently.
    r"[ \t]*(?:=[^\r\n]*?)?;?[ \t]*\r?$"
)


def declared_shapes(clean: str) -> dict[str, dict[str, Any]]:
    """What each class holds, in the shape `model_fields` already has.

    A property counts only when the brace it sits directly inside belongs to a
    class. PowerShell writes a parameter the same way -- `param([String]
    $Method)` -- and without that rule every parameter of every advanced
    function would be reported as a field of whatever class happened to
    enclose it, or of none.

    Only the blanked copy is read. A class name and a type name are bare
    words, so nothing a comment or a here-string holds can be one, and
    `DeliveryOptimizationStatus` documents its own classes in prose above
    them.
    """

    classes = {match.start(): match for match in CLASS.finditer(clean)}
    properties = {match.start("type"): match for match in TYPED_PROPERTY.finditer(clean)}
    fields: dict[str, list[dict[str, Any]]] = {}
    bases: dict[str, list[str]] = {}
    first_line: dict[str, int] = {}

    # `None` marks a brace that is not a class body: a function, a script
    # block, a hashtable. A property inside one belongs to no shape.
    stack: list[str | None] = []
    pending: str | None = None
    position = 0
    length = len(clean)
    while position < length:
        declaration = classes.get(position)
        if declaration is not None:
            pending = declaration.group("name")
            if declaration.group("base"):
                bases[pending] = [declaration.group("base")]
            position = declaration.end()
            continue

        member = properties.get(position + 1)
        if member is not None and stack and stack[-1] is not None:
            owner = stack[-1]
            line = _line_of(clean, member.start("type"))
            fields.setdefault(owner, []).append(
                {
                    "name": member.group("name"),
                    "annotation": member.group("type"),
                    "line": line,
                }
            )
            first_line.setdefault(owner, line)
            position = member.end()
            continue

        character = clean[position]
        if character == "{":
            stack.append(pending)
            pending = None
        elif character == "}" and stack:
            stack.pop()
        position += 1

    return {
        owner: {"fields": members, "line": first_line[owner], "bases": bases.get(owner, [])}
        for owner, members in fields.items()
        if members
    }


EXPORT_MEMBER = re.compile(r"(?i)\bExport-ModuleMember\b")


def publishes_a_module(path: str, clean: str) -> bool:
    """Whether this file offers its functions to anything else.

    A `.psm1` is a module: another script imports it and calls what it
    exports. A `.ps1` is a script: it is run, and its functions are the steps
    it runs, not a surface anybody is meant to reach.

    Reporting the second as public API was true and misleading, which is the
    pair this engine's own audit exists to catch. It flagged
    `tools/test_workorder_status_boundary.ps1` -- a test harness -- as
    publishing thirty functions, on the strength of dot-sourcing being
    possible. Nothing dot-sources it. Across this corpus the claim was wrong
    every time it fired: twenty `.ps1` files, no `.psm1`, and no
    `Export-ModuleMember` anywhere.

    A script that calls `Export-ModuleMember` is deliberately offering a
    surface and counts as a module whatever it is named.
    """

    return path.casefold().endswith(".psm1") or bool(EXPORT_MEMBER.search(clean))


def _excerpt(lines: list[str], line: int, fallback: str) -> str:
    """The source line a receipt hashes, or the path when the line is gone."""

    return lines[line - 1] if 0 < line <= len(lines) else fallback


def _quote(text: str) -> str:
    folded = " ".join(text.split())
    if len(folded) <= MAX_MESSAGE_CHARS:
        return f'"{folded}"'
    return f'"{folded[: MAX_MESSAGE_CHARS - 1]}…"'


class PowerShellLexicalAnalyzer:
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
            if item.language == "PowerShell" and item.size_bytes <= MAX_SOURCE_BYTES
        ]
        analyzed = 0

        def receipt(path: str, start: int, end: int, kind: str, excerpt: str) -> EvidenceRecord:
            record = EvidenceRecord(
                evidence_id=stable_id(
                    "evidence", (snapshot.snapshot_id, path, start, kind, ANALYZER_VERSION)
                ),
                snapshot_id=snapshot.snapshot_id,
                path=path,
                start_line=start,
                end_line=end,
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
            clean = _blank_noise(source)
            functions = declared_functions(clean)
            file_values = declared_values(source, clean)
            file_shapes = declared_shapes(clean)
            file_numbers = {
                name: entry
                for name, entry in file_values.items()
                if declares_a_number(entry["value"])
            }
            file_strings = {
                name: entry for name, entry in file_values.items() if name not in file_numbers
            }
            file_sets = declared_value_sets(source, clean)
            blocks = parameter_blocks(clean)
            throws = throw_sites(source, clean)
            lines = source.splitlines()

            names: dict[str, int] = dict(functions)
            for block in blocks:
                for parameter in block.names:
                    names.setdefault(parameter, block.line)

            symbols.append(
                SymbolRecord(
                    symbol_id=stable_id(
                        "symbol",
                        (snapshot.snapshot_id, file_record.path, "module", ANALYZER_VERSION),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    path=file_record.path,
                    qualified_name=file_record.path,
                    kind="module",
                    start_line=1,
                    end_line=max(1, file_record.line_count),
                    language="PowerShell",
                    analyzer=ANALYZER_VERSION,
                    metadata={
                        "analysis_level": "lexical",
                        **({"name_index": names} if names else {}),
                        **({"tunables": file_numbers} if file_numbers else {}),
                        **({"string_constants": file_strings} if file_strings else {}),
                        **({"collection_constants": file_sets} if file_sets else {}),
                        **({"model_fields": file_shapes} if file_shapes else {}),
                    },
                )
            )
            for name, line in functions.items():
                symbols.append(
                    SymbolRecord(
                        symbol_id=stable_id(
                            "symbol",
                            (snapshot.snapshot_id, file_record.path, name, ANALYZER_VERSION),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        path=file_record.path,
                        qualified_name=name,
                        kind="function",
                        start_line=line,
                        end_line=line,
                        language="PowerShell",
                        analyzer=ANALYZER_VERSION,
                        metadata={},
                    )
                )

            for target, line in imported_modules(source, clean):
                edges.append(
                    EdgeRecord(
                        edge_id=stable_id(
                            "edge",
                            (snapshot.snapshot_id, file_record.path, "imports", target, line),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        source_symbol_id=None,
                        source_path=file_record.path,
                        relationship="imports",
                        target_ref=target,
                        target_symbol_id=None,
                        evidence_id=None,
                        analyzer=ANALYZER_VERSION,
                    )
                )

            if not describes_the_product(file_record.role):
                continue

            script_parameters = [
                name for block in blocks if block.script_level for name in block.names
            ]
            if script_parameters:
                first = next(block.line for block in blocks if block.script_level)
                named = ", ".join(f"`-{name}`" for name in script_parameters[:MAX_NAMED])
                more = (
                    f" and {len(script_parameters) - MAX_NAMED:,} more"
                    if len(script_parameters) > MAX_NAMED
                    else ""
                )
                claim(
                    (
                        f"{file_record.path} declares a command-line interface -- "
                        f"{len(script_parameters):,} parameter(s): {named}{more}. These are "
                        "the words a user types; a script being executable says only that "
                        "it can be started."
                    ),
                    "command_line_interface",
                    "high",
                    receipt(
                        file_record.path,
                        first,
                        first,
                        "command_line_interface",
                        _excerpt(lines, first, file_record.path),
                    ).evidence_id,
                    file_record.path,
                )

            if functions and publishes_a_module(file_record.path, clean):
                first_line = min(functions.values())
                named = ", ".join(f"`{name}`" for name in sorted(functions)[:MAX_NAMED])
                more = (
                    f" and {len(functions) - MAX_NAMED:,} more"
                    if len(functions) > MAX_NAMED
                    else ""
                )
                claim(
                    (
                        f"{file_record.path} publishes {len(functions):,} function(s): "
                        f"{named}{more}. This file is a module, so those names are what "
                        "another script imports and calls."
                    ),
                    "public_api",
                    "medium",
                    receipt(
                        file_record.path,
                        first_line,
                        first_line,
                        "public_api",
                        _excerpt(lines, first_line, file_record.path),
                    ).evidence_id,
                    file_record.path,
                )

            for name, direction, line in dict.fromkeys(environment_reads(source)):
                claim(
                    (
                        f"{file_record.path} {direction} environment setting {name}"
                        + (
                            " at run time. A machine that does not set it runs a different "
                            "script from the one this file describes."
                            if direction == "reads"
                            else ", so every process this script starts inherits the value "
                            "it wrote rather than the one the machine had."
                        )
                    ),
                    "configuration_read",
                    "medium",
                    receipt(
                        file_record.path,
                        line,
                        line,
                        "environment_read",
                        _excerpt(lines, line, file_record.path),
                    ).evidence_id,
                    file_record.path,
                )

            if throws:
                first_line = throws[0][0]
                messages = [message for _, message in throws if message]
                quoted = (
                    " Including " + ", ".join(_quote(item) for item in messages[:3]) + "."
                    if messages
                    else ""
                )
                claim(
                    (
                        f"{file_record.path} throws in {len(throws):,} place(s), which a "
                        "caller sees as a terminating error unless it is caught."
                        f"{quoted}"
                    ),
                    "failure_surface",
                    "medium",
                    receipt(
                        file_record.path,
                        first_line,
                        first_line,
                        "failure_surface",
                        _excerpt(lines, first_line, file_record.path),
                    ).evidence_id,
                    file_record.path,
                )

        coverage = CoverageRecord(
            analyzer=ANALYZER_VERSION,
            language="PowerShell",
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
