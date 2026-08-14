# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""The SQL schema reader against SQLite itself.

This is the cheapest honest oracle in the project. Every other differential
here needs a toolchain that may not be installed; this one needs `sqlite3`,
which ships with Python. The reference is not another parser's opinion -- it
is the database the DDL actually produces, read back through `PRAGMA`.

As with every differential in this directory, a disagreement says the two
readers differ. It does not say which one is wrong. The first run found the
reader missing `CREATE VIRTUAL TABLE`, and also found ten "missing" tables
that were FTS5 shadow tables the reader is correct to omit -- nobody declared
them. Both showed up as the same kind of line.

Usage:
    python benchmarks/differential/run_sql_differential.py --sql schema.sql
    python benchmarks/differential/run_sql_differential.py --source src/x.py
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.analyzers.sql_schema import (
    parse_indexes,
    parse_tables,
    strip_comments,
)

# SQLite creates these for a virtual table's own storage. They are real tables
# in the database and declared by no one, so the reader omitting them is the
# correct behaviour rather than a miss.
SHADOW_SUFFIXES = ("_config", "_content", "_data", "_docsize", "_idx")

STATEMENT = re.compile(
    r"CREATE\s+(?:TEMP\w*\s+|VIRTUAL\s+|UNIQUE\s+)*(?:TABLE|INDEX)\b[^(;]*\(",
    re.IGNORECASE,
)


def _executable_statements(text: str) -> list[str]:
    """DDL statements that SQLite can be asked to run.

    Application code holds DDL inside string literals, so statements are
    recovered by pattern rather than by taking the file whole. A statement ends
    at its balanced closing paren, not at a semicolon: DDL embedded in a string
    frequently carries no terminator, and scanning to the next `;` ran off the
    end of the literal and swallowed the surrounding code, which then failed to
    execute and reported the table as missing from SQLite.

    This scan is deliberately written here rather than imported from the
    analyzer. An oracle that shares its extraction with the reader it checks
    cannot detect an extraction bug in either of them.
    """

    statements: list[str] = []
    for match in STATEMENT.finditer(text):
        depth = 0
        index = match.end() - 1
        while index < len(text):
            char = text[index]
            if char == "'":
                index += 1
                while index < len(text) and text[index] != "'":
                    index += 1
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    statements.append(text[match.start() : index + 1] + ";")
                    break
            index += 1
    return statements


def _reference(
    statements: list[str],
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, list[str]], list[str]]:
    """SQLite's reading of the schema, and the statements it agreed to run.

    The accepted statements are returned so the reader can be given exactly
    the same input. A file holding unrelated DDL snippets -- this project's own
    tests, for one -- re-declares the same index name against different tables,
    and SQLite keeps the first and rejects the rest. Comparing the reader's
    view of every statement against a database built from a subset reports the
    difference between the two inputs, not between the two readers.
    """

    connection = sqlite3.connect(":memory:")
    accepted: list[str] = []
    for statement in statements:
        try:
            connection.execute(statement)
        except sqlite3.Error:
            # A statement referencing a table declared elsewhere, or dialect
            # SQLite does not accept, is not a disagreement to report.
            continue
        accepted.append(statement)
    tables: dict[str, list[str]] = {}
    keys: dict[str, list[str]] = {}
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        name = str(row[0])
        if name.endswith(SHADOW_SUFFIXES):
            continue
        info = list(connection.execute(f"PRAGMA table_info('{name}')"))
        tables[name] = [str(item[1]) for item in info]
        keys[name] = [str(item[1]) for item in info if item[5]]
    indexes = {
        str(row[0]): [str(item[2]) for item in connection.execute(f"PRAGMA index_info('{row[0]}')")]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        )
    }
    connection.close()
    return tables, keys, indexes, accepted


def compare(text: str) -> list[str]:
    """Every disagreement between the reader and SQLite, as readable lines."""

    statements = _executable_statements(text)
    if not statements:
        return []
    reference_tables, reference_keys, reference_indexes, accepted = _reference(statements)
    joined = "\n".join(accepted)
    mine_tables = {table.name: table for table in parse_tables(joined)}
    mine_indexes = {index.name: index for index in parse_indexes(joined)}

    findings: list[str] = []
    for name in sorted(set(reference_tables) - set(mine_tables)):
        findings.append(f"table {name}: sqlite declares it, the reader does not")
    for name in sorted(set(mine_tables) - set(reference_tables)):
        findings.append(f"table {name}: the reader declares it, sqlite does not")
    for name in sorted(set(reference_tables) & set(mine_tables)):
        mine = [column.name for column in mine_tables[name].columns]
        if mine != reference_tables[name]:
            findings.append(
                f"table {name} columns: sqlite {reference_tables[name]} / reader {mine}"
            )
        if sorted(mine_tables[name].primary_key) != sorted(reference_keys[name]):
            findings.append(
                f"table {name} key: sqlite {reference_keys[name]} / "
                f"reader {list(mine_tables[name].primary_key)}"
            )
    for name in sorted(set(reference_indexes) ^ set(mine_indexes)):
        side = "sqlite" if name in reference_indexes else "the reader"
        findings.append(f"index {name}: only {side} declares it")
    for name in sorted(set(reference_indexes) & set(mine_indexes)):
        if list(mine_indexes[name].columns) != reference_indexes[name]:
            findings.append(
                f"index {name} columns: sqlite {reference_indexes[name]} / "
                f"reader {list(mine_indexes[name].columns)}"
            )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, action="append", default=[])
    parser.add_argument("--root", type=Path)
    arguments = parser.parse_args()

    targets = list(arguments.source)
    if arguments.root:
        targets.extend(
            path
            for pattern in ("*.sql", "*.py")
            for path in arguments.root.rglob(pattern)
            if ".git" not in path.parts and "node_modules" not in path.parts
        )
    if not targets:
        parser.error("pass --source or --root")

    total = 0
    examined = 0
    for path in targets:
        try:
            text = strip_comments(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        findings = compare(text)
        if not _executable_statements(text):
            continue
        examined += 1
        for line in findings:
            print(f"{path}: {line}")
        total += len(findings)
    print(f"files_with_ddl={examined} disagreements={total}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
