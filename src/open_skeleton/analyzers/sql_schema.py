# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""The shape of relational schemas, wherever the DDL is written.

Schema is one of the few things in a codebase that is fully declared rather
than inferred, and it was the largest thing this engine walked past. A reader
asking "what does this system store" was told which table names appear and
nothing else: not the columns, not which of them may be null, not the primary
key, not whether there is one, and not a single index.

This reads the DDL itself, and it does so without caring which language holds
it. Schema lives in `.sql` migrations in some repositories and inside string
literals in application code in others, and both are just text at this level.
That is why eligibility here is "the file contains DDL" rather than "the file
is SQL" -- a rule keyed on the extension would have found nothing at all in
the repository this was written against.

Three lexical details are handled explicitly, because each one silently
corrupts every count that follows:

* **A `--` inside a string literal is not a comment.** `DEFAULT '--'` ends the
  column list early for a scanner that strips comments first and asks
  questions later.
* **Commas nest.** `CHECK (a IN (1, 2))` is one column definition containing
  two commas, and splitting the body on every comma reports it as three.
* **Identifiers may be quoted, bracketed, backticked, or schema-qualified.**
  `"order"` is a legal table name precisely because `order` is not.

What this deliberately does not do is decide whether a schema is *good*. It
reports that a table declares no primary key; it does not say that it should.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from open_skeleton.ids import stable_id
from open_skeleton.models import (
    AnalysisResult,
    ClaimRecord,
    CoverageRecord,
    EdgeRecord,
    EvidenceRecord,
    FileRecord,
    Snapshot,
    SymbolRecord,
    utc_now,
)

ANALYZER_VERSION = "sql-schema/v1"

# Roles whose DDL describes something other than this system's storage.
# Documentation carries illustrative schemas in fenced blocks, and treating a
# README's example table as a declared one is how a repository acquires tables
# it does not have.
INELIGIBLE_ROLES = frozenset({"documentation", "unknown"})

# A cheap prefilter. Reading every source file to look for schema is affordable
# only because the overwhelming majority are rejected by a substring test
# before anything is parsed.
DDL_HINT = re.compile(r"create\s+(?:temp\w*\s+)?(?:table|(?:unique\s+)?index)", re.IGNORECASE)

_IDENT = r'(?:"[^"]*"|`[^`]*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9$]*)'
_QUALIFIED = rf"{_IDENT}(?:\s*\.\s*{_IDENT})*"

TABLE_START = re.compile(
    rf"\bCREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?P<name>{_QUALIFIED})\s*\(",
    re.IGNORECASE,
)
INDEX_START = re.compile(
    rf"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?P<name>{_QUALIFIED})\s+ON\s+(?P<table>{_QUALIFIED})\s*\(",
    re.IGNORECASE,
)
UNIQUE_INDEX = re.compile(r"\bCREATE\s+UNIQUE\s+INDEX\b", re.IGNORECASE)
VIRTUAL_START = re.compile(
    rf"\bCREATE\s+VIRTUAL\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?P<name>{_QUALIFIED})\s+USING\s+(?P<module>{_IDENT})\s*\(",
    re.IGNORECASE,
)

# A full-text index declares the searchable columns and then its own storage
# options in the same argument list. Only the former are columns.
MODULE_OPTION = re.compile(r"^\s*[A-Za-z_][\w]*\s*=")

# A definition that opens with one of these is a table-level constraint rather
# than a column. `CONSTRAINT` is included because a named constraint puts its
# own identifier first and would otherwise be counted as a column.
TABLE_CONSTRAINT_OPENERS = frozenset(
    {"primary", "foreign", "unique", "check", "constraint", "exclude"}
)

REFERENCES = re.compile(rf"\bREFERENCES\s+(?P<table>{_QUALIFIED})", re.IGNORECASE)
PRIMARY_KEY_COLUMNS = re.compile(r"\bPRIMARY\s+KEY\s*\((?P<columns>[^)]*)\)", re.IGNORECASE)

MAX_SOURCE_BYTES = 4_000_000

# Below two indexes there is no shared pattern to report, only a restatement of
# the single index already claimed on its own.
MIN_INDEXES_FOR_A_PATTERN = 2


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    not_null: bool
    primary_key: bool
    references: str | None


@dataclass(slots=True)
class Table:
    name: str
    line: int
    columns: list[Column] = field(default_factory=list)
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[str, ...] = ()
    checks: int = 0
    unique_constraints: int = 0
    # The virtual-table module, when the table is one. A virtual table has no
    # keys or constraints of its own -- the module owns its storage -- so the
    # shape reported for it is a different statement entirely.
    module: str | None = None


@dataclass(frozen=True, slots=True)
class Index:
    name: str
    table: str
    columns: tuple[str, ...]
    unique: bool
    line: int


def _unquote(name: str) -> str:
    """The bare identifier, without quoting or schema qualification.

    `main."order"` and `` `order` `` and `[order]` all name the same table, and
    a reader comparing an index's target against a declared table needs them to
    compare equal.
    """

    segment = name.strip().split(".")[-1].strip()
    if len(segment) >= 2 and segment[0] in '"`[' and segment[-1] in '"`]':
        return segment[1:-1].strip()
    return segment


def strip_comments(text: str) -> str:
    """Blank out SQL comments while preserving every line and column offset.

    Offsets are preserved rather than the text rewritten because every match
    position found afterwards is converted back into a line number for a
    receipt. Replacing a comment with nothing would move every claim below it.
    """

    out = list(text)
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "'":
            index += 1
            while index < length:
                if text[index] == "'":
                    # Doubled quotes are an escaped quote, not a terminator.
                    if index + 1 < length and text[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char == "-" and index + 1 < length and text[index + 1] == "-":
            while index < length and text[index] != "\n":
                out[index] = " "
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            while index < length and not (
                text[index] == "*" and index + 1 < length and text[index + 1] == "/"
            ):
                if text[index] != "\n":
                    out[index] = " "
                index += 1
            for offset in range(index, min(index + 2, length)):
                out[offset] = " "
            index += 2
            continue
        index += 1
    return "".join(out)


def _balanced(text: str, opening: int) -> tuple[str, int] | None:
    """The body of the parenthesised group beginning at ``opening``.

    Returns the inner text and the offset just past the closing paren, or
    ``None`` when the group never closes -- which happens whenever a heuristic
    match landed on something that was not DDL at all.
    """

    depth = 0
    index = opening
    length = len(text)
    while index < length:
        char = text[index]
        if char == "'":
            index += 1
            while index < length and text[index] != "'":
                index += 1
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index], index + 1
        index += 1
    return None


def split_definitions(body: str) -> list[str]:
    """Top-level comma-separated parts of a column list.

    `CHECK (state IN ('a', 'b'))` is one definition holding two commas.
    """

    parts: list[str] = []
    depth = 0
    current: list[str] = []
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char == "'":
            current.append(char)
            index += 1
            while index < length:
                current.append(body[index])
                if body[index] == "'":
                    index += 1
                    break
                index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    if "".join(current).strip():
        parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _column_list(raw: str) -> tuple[str, ...]:
    """Column names from an index or key definition, order preserved.

    Order is the whole point for an index: a reader asking which queries an
    index can serve is asking what it leads with.
    """

    names: list[str] = []
    for part in split_definitions(raw):
        # `col DESC` and `col COLLATE NOCASE` carry the name first.
        head = part.split()[0] if part.split() else ""
        if head:
            names.append(_unquote(head))
    return tuple(names)


def parse_tables(text: str) -> list[Table]:
    """Every `CREATE TABLE` in ``text``, with its columns and constraints."""

    tables: list[Table] = []
    for match in TABLE_START.finditer(text):
        group = _balanced(text, match.end() - 1)
        if group is None:
            continue
        body, _ = group
        table = Table(
            name=_unquote(match.group("name")), line=text.count("\n", 0, match.start()) + 1
        )
        primary: list[str] = []
        foreign: list[str] = []
        for definition in split_definitions(body):
            words = definition.split()
            if not words:
                continue
            opener = words[0].strip("(").casefold()
            if opener in TABLE_CONSTRAINT_OPENERS:
                folded = definition.casefold()
                if "primary key" in folded:
                    found = PRIMARY_KEY_COLUMNS.search(definition)
                    if found:
                        primary.extend(_column_list(found.group("columns")))
                elif "foreign key" in folded:
                    target = REFERENCES.search(definition)
                    if target:
                        foreign.append(_unquote(target.group("table")))
                elif folded.startswith("check"):
                    table.checks += 1
                elif folded.startswith("unique"):
                    table.unique_constraints += 1
                continue
            folded = definition.casefold()
            target = REFERENCES.search(definition)
            inline_key = "primary key" in folded
            column = Column(
                name=_unquote(words[0]),
                not_null="not null" in folded,
                primary_key=inline_key,
                references=_unquote(target.group("table")) if target else None,
            )
            table.columns.append(column)
            if inline_key:
                primary.append(column.name)
            if "check" in folded and "(" in definition:
                table.checks += 1
            if target:
                foreign.append(column.references or "")
        table.primary_key = tuple(primary)
        table.foreign_keys = tuple(sorted({item for item in foreign if item}))
        tables.append(table)
    tables.extend(_virtual_tables(text))
    return tables


def _virtual_tables(text: str) -> list[Table]:
    """`CREATE VIRTUAL TABLE` declarations, which the plain form never matches.

    A differential against a live SQLite database found these missing: the
    engine's own full-text search over claims and files is declared this way,
    so a document built from the plain form omitted the search surface while
    reporting every table it is built on.

    The shadow tables a module creates for itself are deliberately not
    reported. They exist in the database and are declared by nobody.
    """

    found: list[Table] = []
    for match in VIRTUAL_START.finditer(text):
        group = _balanced(text, match.end() - 1)
        if group is None:
            continue
        body, _ = group
        columns = [
            Column(
                name=_unquote(part.split()[0]), not_null=False, primary_key=False, references=None
            )
            for part in split_definitions(body)
            if part.split() and not MODULE_OPTION.match(part)
        ]
        found.append(
            Table(
                name=_unquote(match.group("name")),
                line=text.count("\n", 0, match.start()) + 1,
                columns=columns,
                module=_unquote(match.group("module")),
            )
        )
    return found


def parse_indexes(text: str) -> list[Index]:
    """Every `CREATE INDEX` in ``text``, with the columns it covers in order."""

    indexes: list[Index] = []
    for match in INDEX_START.finditer(text):
        group = _balanced(text, match.end() - 1)
        if group is None:
            continue
        body, _ = group
        columns = _column_list(body)
        if not columns:
            continue
        indexes.append(
            Index(
                name=_unquote(match.group("name")),
                table=_unquote(match.group("table")),
                columns=columns,
                unique=bool(UNIQUE_INDEX.match(text, match.start())),
                line=text.count("\n", 0, match.start()) + 1,
            )
        )
    return indexes


class SqlSchemaAnalyzer:
    """Relational schema facts read from DDL, in any file that carries it."""

    name = "sql-schema"
    version = "v1"

    def analyze(self, snapshot: Snapshot) -> AnalysisResult:
        started = time.perf_counter()
        created_at = utc_now()
        symbols: list[SymbolRecord] = []
        edges: list[EdgeRecord] = []
        evidence: list[EvidenceRecord] = []
        claims: list[ClaimRecord] = []
        failures: list[str] = []

        candidates = [
            item
            for item in snapshot.files
            if str(item.role) not in INELIGIBLE_ROLES and item.size_bytes <= MAX_SOURCE_BYTES
        ]

        eligible: list[tuple[FileRecord, str]] = []
        for file_record in candidates:
            source_path = snapshot.root / Path(file_record.path)
            try:
                payload = source_path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != file_record.sha256:
                    raise ValueError("content changed after snapshot")
                source = payload.decode("utf-8", errors="strict")
            except (OSError, UnicodeDecodeError, ValueError):
                # A file that cannot be read is not eligible for a schema it was
                # never shown to contain. Counting every unreadable file in the
                # repository as a schema failure would put the coverage of this
                # analyzer at the mercy of unrelated binary assets.
                continue
            if DDL_HINT.search(source):
                eligible.append((file_record, source))

        def receipt(
            path: str, lines: list[str], line: int, kind: str, symbol: str | None
        ) -> EvidenceRecord:
            """A receipt for one declaration, cited at the line that makes it.

            Defined outside the file loop and given its file explicitly. As a
            closure over the loop variables it read whichever file the loop had
            reached, which is correct only for as long as nobody defers a call.
            """

            excerpt = lines[line - 1] if 0 < line <= len(lines) else ""
            record = EvidenceRecord(
                evidence_id=stable_id(
                    "evidence",
                    (snapshot.snapshot_id, path, line, kind, symbol, ANALYZER_VERSION),
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

        analyzed_files = 0
        for file_record, source in eligible:
            path = file_record.path
            role = str(file_record.role)
            try:
                text = strip_comments(source)
                tables = parse_tables(text)
                indexes = parse_indexes(text)
            except (RecursionError, ValueError) as exc:
                failures.append(f"{path}: {exc.__class__.__name__}: {exc}")
                continue
            if not tables and not indexes:
                # The prefilter matched a phrase that was not DDL. The file was
                # read successfully, so it counts as analyzed and simply had
                # nothing to say.
                analyzed_files += 1
                continue
            analyzed_files += 1
            lines = source.splitlines()
            fixture = role == "test"

            for table in tables:
                table_symbol = stable_id(
                    "symbol", (snapshot.snapshot_id, path, "table", table.name, ANALYZER_VERSION)
                )
                symbols.append(
                    SymbolRecord(
                        symbol_id=table_symbol,
                        snapshot_id=snapshot.snapshot_id,
                        path=path,
                        qualified_name=table.name,
                        kind="table",
                        start_line=table.line,
                        end_line=table.line,
                        language="SQL",
                        analyzer=ANALYZER_VERSION,
                        metadata={
                            "analysis_level": "declared",
                            "columns": [column.name for column in table.columns],
                            "primary_key": list(table.primary_key),
                        },
                    )
                )
                mark = receipt(path, lines, table.line, "sql_table", table.name)
                if table.module:
                    shape = (
                        f"Table `{table.name}` is a virtual table backed by the "
                        f"`{table.module}` module over ({
                            ', '.join(column.name for column in table.columns)
                        })."
                    )
                else:
                    nullable = sum(1 for column in table.columns if not column.not_null)
                    shape = (
                        f"Table `{table.name}` is declared with {len(table.columns):,} "
                        f"column(s), {nullable:,} of which may be null"
                    )
                    if table.primary_key:
                        shape += f", keyed on ({', '.join(table.primary_key)})"
                    if table.checks:
                        shape += f", under {table.checks:,} CHECK constraint(s)"
                    shape += "."
                if fixture:
                    shape += " It is created as a test fixture."
                claims.append(
                    self._claim(
                        snapshot,
                        created_at,
                        text=shape,
                        category="storage_schema",
                        supporting=(mark.evidence_id,),
                        importance="high",
                        path=path,
                    )
                )
                # A virtual table is excluded rather than merely unkeyed: its
                # module owns row identity, so "declares no primary key" would
                # report a property of the module as a gap in the schema.
                if not table.primary_key and not fixture and not table.module:
                    claims.append(
                        self._claim(
                            snapshot,
                            created_at,
                            text=(
                                f"Table `{table.name}` declares no primary key, so no column "
                                "or column group in it is stated to identify a row uniquely."
                            ),
                            category="storage_schema",
                            supporting=(mark.evidence_id,),
                            importance="high",
                            path=path,
                        )
                    )
                for target in table.foreign_keys:
                    edges.append(
                        EdgeRecord(
                            edge_id=stable_id(
                                "edge",
                                (
                                    snapshot.snapshot_id,
                                    table_symbol,
                                    "references_table",
                                    target,
                                    ANALYZER_VERSION,
                                ),
                            ),
                            snapshot_id=snapshot.snapshot_id,
                            source_symbol_id=table_symbol,
                            source_path=path,
                            relationship="references_table",
                            target_ref=target,
                            target_symbol_id=None,
                            evidence_id=mark.evidence_id,
                            analyzer=ANALYZER_VERSION,
                        )
                    )

            for index in indexes:
                index_symbol = stable_id(
                    "symbol", (snapshot.snapshot_id, path, "index", index.name, ANALYZER_VERSION)
                )
                symbols.append(
                    SymbolRecord(
                        symbol_id=index_symbol,
                        snapshot_id=snapshot.snapshot_id,
                        path=path,
                        qualified_name=index.name,
                        kind="index",
                        start_line=index.line,
                        end_line=index.line,
                        language="SQL",
                        analyzer=ANALYZER_VERSION,
                        metadata={
                            "analysis_level": "declared",
                            "table": index.table,
                            "columns": list(index.columns),
                        },
                    )
                )
                mark = receipt(path, lines, index.line, "sql_index", index.name)
                kind = "Unique index" if index.unique else "Index"
                claims.append(
                    self._claim(
                        snapshot,
                        created_at,
                        text=(
                            f"{kind} `{index.name}` covers `{index.table}` "
                            f"({', '.join(index.columns)})."
                        ),
                        category="storage_schema",
                        supporting=(mark.evidence_id,),
                        importance="medium",
                        path=path,
                    )
                )
                edges.append(
                    EdgeRecord(
                        edge_id=stable_id(
                            "edge",
                            (
                                snapshot.snapshot_id,
                                index_symbol,
                                "indexes_table",
                                index.table,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        source_symbol_id=index_symbol,
                        source_path=path,
                        relationship="indexes_table",
                        target_ref=index.table,
                        target_symbol_id=None,
                        evidence_id=mark.evidence_id,
                        analyzer=ANALYZER_VERSION,
                    )
                )

            if len(indexes) >= MIN_INDEXES_FOR_A_PATTERN and not fixture:
                leading = Counter(index.columns[0] for index in indexes)
                column, count = leading.most_common(1)[0]
                if count == len(indexes):
                    claims.append(
                        self._claim(
                            snapshot,
                            created_at,
                            text=(
                                f"All {len(indexes):,} index(es) declared in `{path}` lead with "
                                f"column `{column}`."
                            ),
                            category="storage_schema",
                            supporting=(
                                receipt(
                                    path, lines, indexes[0].line, "sql_index_pattern", column
                                ).evidence_id,
                            ),
                            importance="medium",
                            path=path,
                        )
                    )

        coverage = CoverageRecord(
            analyzer=ANALYZER_VERSION,
            language="SQL",
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
        importance: str,
        path: str,
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
            invalidation_keys=(f"file:{path}",),
            alternative_hypotheses=(
                (
                    "This is the DDL as written, not the schema as deployed. A statement "
                    "reached only on one branch, a migration that alters this table later, "
                    "and a database created by a different tool are all invisible here."
                ),
            ),
        )
