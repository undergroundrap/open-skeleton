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
same `command_line_interface` claim rather than a new category. A `function`
is public surface. A `throw` is what can come out of a call that is not a
value.

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

from open_skeleton.ids import stable_id
from open_skeleton.models import (
    AnalysisResult,
    ClaimRecord,
    CoverageRecord,
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


def _blank_noise(source: str) -> str:
    """Replace comments and string bodies with spaces, keeping every newline.

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

            if functions:
                first_line = min(functions.values())
                named = ", ".join(f"`{name}`" for name in sorted(functions)[:MAX_NAMED])
                more = (
                    f" and {len(functions) - MAX_NAMED:,} more"
                    if len(functions) > MAX_NAMED
                    else ""
                )
                claim(
                    (
                        f"{file_record.path} defines {len(functions):,} function(s): "
                        f"{named}{more}. PowerShell dot-sourcing makes every one of them "
                        "callable by any script that loads this file."
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
            edges=(),
            evidence=tuple(evidence),
            claims=tuple(claims),
            coverage=(coverage,),
        )
