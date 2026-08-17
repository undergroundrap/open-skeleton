# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""The DDL reader, against the lexical cases that silently corrupt a count.

Every case here is one where a wrong answer looks like a right one: a table
with the wrong number of columns reads exactly as plausibly as the correct
count, and nothing downstream can tell. The differential in
`benchmarks/differential/run_sql_differential.py` checks the same reader
against SQLite itself; these are the shapes a repository may not happen to
contain on any given day.
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.sql_schema import (
    ANALYZER_VERSION,
    parse_indexes,
    parse_statements,
    parse_tables,
    split_definitions,
    strip_comments,
)
from open_skeleton.scanner import scan_repository

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "differential"))


class CommentTests(TestCase):
    def test_a_comment_marker_inside_a_string_is_not_a_comment(self) -> None:
        # `DEFAULT '--'` ends the column list early for a scanner that strips
        # comments without tracking string literals, and the table then reports
        # one column instead of two.
        sql = "CREATE TABLE t (a TEXT DEFAULT '--', b TEXT NOT NULL);"
        tables = parse_tables(strip_comments(sql))
        self.assertEqual([column.name for column in tables[0].columns], ["a", "b"])

    def test_a_line_comment_is_removed_without_moving_any_line(self) -> None:
        # Receipts are line numbers computed after stripping, so a comment that
        # collapses to nothing shifts every claim below it onto the wrong line.
        sql = "-- leading note\nCREATE TABLE t (a TEXT);"
        self.assertEqual(strip_comments(sql).count("\n"), sql.count("\n"))
        self.assertEqual(parse_tables(strip_comments(sql))[0].line, 2)

    def test_a_block_comment_between_columns_is_removed(self) -> None:
        sql = "CREATE TABLE t (a TEXT, /* note, with a comma */ b TEXT);"
        tables = parse_tables(strip_comments(sql))
        self.assertEqual([column.name for column in tables[0].columns], ["a", "b"])


class DefinitionSplitTests(TestCase):
    def test_a_nested_comma_does_not_split_a_definition(self) -> None:
        self.assertEqual(
            split_definitions("a TEXT CHECK (a IN ('x', 'y')), b TEXT"),
            ["a TEXT CHECK (a IN ('x', 'y'))", "b TEXT"],
        )

    def test_a_comma_inside_a_string_does_not_split(self) -> None:
        self.assertEqual(
            split_definitions("a TEXT DEFAULT 'x,y', b TEXT"), ["a TEXT DEFAULT 'x,y'", "b TEXT"]
        )


class TableTests(TestCase):
    def test_columns_constraints_and_keys_are_counted_separately(self) -> None:
        sql = """
        CREATE TABLE orders (
            id TEXT NOT NULL,
            customer TEXT NOT NULL REFERENCES customers(id),
            note TEXT,
            CHECK (length(id) > 0),
            PRIMARY KEY (id, customer)
        );
        """
        table = parse_tables(strip_comments(sql))[0]
        self.assertEqual([column.name for column in table.columns], ["id", "customer", "note"])
        self.assertEqual(table.primary_key, ("id", "customer"))
        self.assertEqual([key.table for key in table.foreign_keys], ["customers"])
        self.assertEqual(table.checks, 1)

    def test_a_named_constraint_is_not_counted_as_a_column(self) -> None:
        # `CONSTRAINT ok CHECK (...)` puts its own identifier first, so a
        # reader taking the first token of every definition invents a column
        # called `CONSTRAINT`.
        sql = "CREATE TABLE t (a TEXT, CONSTRAINT ok CHECK (a <> ''));"
        table = parse_tables(strip_comments(sql))[0]
        self.assertEqual([column.name for column in table.columns], ["a"])

    def test_a_quoted_identifier_is_read_without_its_quotes(self) -> None:
        # `order` is a reserved word, which is exactly why a table is named
        # `"order"`. An index naming the same table unquoted must still match.
        sql = 'CREATE TABLE "order" (a TEXT); CREATE INDEX i ON [order] (a);'
        self.assertEqual(parse_tables(strip_comments(sql))[0].name, "order")
        self.assertEqual(parse_indexes(strip_comments(sql))[0].table, "order")

    def test_a_schema_qualified_name_keeps_only_the_table(self) -> None:
        sql = "CREATE TABLE main.t (a TEXT);"
        self.assertEqual(parse_tables(strip_comments(sql))[0].name, "t")

    def test_an_inline_primary_key_is_recorded(self) -> None:
        sql = "CREATE TABLE t (id TEXT PRIMARY KEY, a TEXT);"
        self.assertEqual(parse_tables(strip_comments(sql))[0].primary_key, ("id",))

    def test_an_unclosed_statement_is_skipped_rather_than_guessed(self) -> None:
        self.assertEqual(parse_tables(strip_comments("CREATE TABLE t (a TEXT")), [])


class ReferentialActionTests(TestCase):
    """What happens to a child row when its parent goes is declared, not implied.

    A schema whose references all cascade behaves differently under a delete
    from one that nulls them, and the clause saying which sits right there in
    the DDL. Reading only the target table threw that away.
    """

    # Each statement below is one unbroken string on purpose. The SQLite
    # differential scans this file's own text, and a statement split across
    # implicit Python concatenation reaches it with the quotes and newline
    # embedded -- which both readers then misparse, differently, and report as
    # a disagreement about the reader.
    def test_the_action_is_read_from_the_clause(self) -> None:
        sql = "CREATE TABLE kid (a TEXT REFERENCES parent(id) ON DELETE CASCADE, b TEXT REFERENCES other(id) ON DELETE SET NULL);"
        keys = parse_tables(strip_comments(sql))[0].foreign_keys
        self.assertEqual(
            {(key.table, key.on_delete) for key in keys},
            {("parent", "CASCADE"), ("other", "SET NULL")},
        )

    def test_an_omitted_clause_is_recorded_as_the_sql_default(self) -> None:
        # Silence means `NO ACTION` -- the delete is refused. "Nothing was
        # said" and "nothing happens" are the same outcome and not the same
        # statement, so the default is named rather than left blank.
        sql = "CREATE TABLE lone (a TEXT REFERENCES parent(id));"
        self.assertEqual(
            parse_tables(strip_comments(sql))[0].foreign_keys[0].on_delete, "NO ACTION"
        )

    def test_a_table_level_foreign_key_carries_its_action_too(self) -> None:
        sql = (
            "CREATE TABLE ward (a TEXT, FOREIGN KEY (a) REFERENCES parent(id) ON DELETE RESTRICT);"
        )
        keys = parse_tables(strip_comments(sql))[0].foreign_keys
        self.assertEqual((keys[0].table, keys[0].on_delete), ("parent", "RESTRICT"))

    def test_the_action_of_one_column_does_not_leak_to_another(self) -> None:
        # The clause is searched from the end of its own `REFERENCES`, not
        # anywhere in the definition, so a cascade on one column cannot be
        # read as a cascade on the column beside it.
        sql = "CREATE TABLE pair (a TEXT REFERENCES p(id), b TEXT REFERENCES q(id) ON DELETE CASCADE);"
        keys = {
            key.table: key.on_delete for key in parse_tables(strip_comments(sql))[0].foreign_keys
        }
        self.assertEqual(keys, {"p": "NO ACTION", "q": "CASCADE"})


class StatementTests(TestCase):
    """What the code does with the rows, not only how the table was declared.

    This reader scans raw source text, so the dominant failure is prose that
    happens to pair `select` with `from`. A React component containing
    "Invalid selection. Type a number from the list" was reported as issuing
    two SELECTs against a table named `the`.
    """

    def _statements(self, sql: str) -> dict[str, dict[str, int]]:
        return {
            verb: dict(counts) for verb, counts in parse_statements(strip_comments(sql)).items()
        }

    def test_each_verb_names_the_table_it_acts_on(self) -> None:
        sql = (
            "SELECT data FROM players WHERE id = ?; "
            "INSERT OR REPLACE INTO players VALUES (?, ?); "
            "UPDATE zones SET name = ? WHERE id = ?; "
            "DELETE FROM zones WHERE id = ?;"
        )
        self.assertEqual(
            self._statements(sql),
            {
                "SELECT": {"players": 1},
                "INSERT": {"players": 1},
                "UPDATE": {"zones": 1},
                "DELETE": {"zones": 1},
            },
        )

    def test_prose_pairing_select_with_from_is_not_a_query(self) -> None:
        prose = 'addLog("Invalid selection. Type a number from the list, or new to start")'
        self.assertEqual(self._statements(prose), {})

    def test_an_article_is_never_a_table_name(self) -> None:
        self.assertEqual(self._statements("please select an item from the shelf and go"), {})

    def test_a_clause_is_required_after_the_table(self) -> None:
        # A statement has to look like one. This costs a bare `SELECT * FROM t`
        # with no clause at all, which is the trade taken: a table nobody
        # queries is worse to print than a query nobody lists.
        self.assertEqual(self._statements("SELECT name FROM widgets"), {})
        self.assertEqual(
            self._statements("SELECT name FROM widgets WHERE id = 1"), {"SELECT": {"widgets": 1}}
        )

    def test_the_clause_is_looked_for_where_the_table_ends(self) -> None:
        # `pattern.match(text, pos)` anchors at `pos`, but `^` in the pattern
        # still means the start of the whole string -- so an anchored guard
        # never fired and rejected every real statement in the file.
        sql = "x = 1\ny = 2\nquery = 'SELECT data FROM players WHERE id = ?'\n"
        self.assertEqual(self._statements(sql), {"SELECT": {"players": 1}})

    def test_a_join_counts_the_table_after_from(self) -> None:
        sql = "SELECT a.x FROM players AS a JOIN zones AS b ON a.z = b.id WHERE a.id = ?"
        self.assertEqual(self._statements(sql)["SELECT"], {"players": 1})


class IndexTests(TestCase):
    def test_column_order_is_preserved(self) -> None:
        # Order is the entire content of an index fact: which queries it can
        # serve is decided by what it leads with.
        sql = "CREATE INDEX i ON t (b, a, c);"
        self.assertEqual(parse_indexes(strip_comments(sql))[0].columns, ("b", "a", "c"))

    def test_a_sort_direction_is_not_mistaken_for_a_column(self) -> None:
        sql = "CREATE INDEX i ON t (a DESC, b COLLATE NOCASE);"
        self.assertEqual(parse_indexes(strip_comments(sql))[0].columns, ("a", "b"))

    def test_a_unique_index_is_distinguished(self) -> None:
        self.assertTrue(parse_indexes(strip_comments("CREATE UNIQUE INDEX i ON t (a);"))[0].unique)
        self.assertFalse(parse_indexes(strip_comments("CREATE INDEX i ON t (a);"))[0].unique)


class VirtualTableTests(TestCase):
    def test_a_virtual_table_is_found_and_names_its_module(self) -> None:
        # Found by the SQLite differential: the plain form never matches this,
        # so the engine's own full-text search was missing from a document
        # that listed every table it is built on.
        sql = "CREATE VIRTUAL TABLE s USING fts5(title, body, content='docs');"
        table = next(item for item in parse_tables(strip_comments(sql)) if item.module)
        self.assertEqual(table.name, "s")
        self.assertEqual(table.module, "fts5")

    def test_a_module_option_is_not_counted_as_a_column(self) -> None:
        sql = "CREATE VIRTUAL TABLE s USING fts5(title, body, content='docs', tokenize='porter');"
        table = next(item for item in parse_tables(strip_comments(sql)) if item.module)
        self.assertEqual([column.name for column in table.columns], ["title", "body"])


class AnalyzerTests(TestCase):
    def _claims(self, sources: dict[str, str]) -> list[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            for name, body in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            return [claim.claim for claim in result.claims if claim.produced_by == ANALYZER_VERSION]

    def test_ddl_in_a_string_literal_is_read(self) -> None:
        # Schema lives in application code more often than in `.sql` files, and
        # an analyzer keyed on the extension finds nothing in such a tree.
        found = self._claims(
            {"store.py": 'SCHEMA = """\nCREATE TABLE t (id TEXT PRIMARY KEY, a TEXT);\n"""\n'}
        )
        self.assertTrue(any("Table `t` is declared with 2 column(s)" in item for item in found))

    def test_a_table_with_no_primary_key_is_reported(self) -> None:
        found = self._claims({"store.sql": "CREATE TABLE t (a TEXT, b TEXT);"})
        self.assertTrue(any("declares no primary key" in item for item in found))

    def test_a_keyed_table_is_not_reported_as_unkeyed(self) -> None:
        found = self._claims({"store.sql": "CREATE TABLE t (a TEXT PRIMARY KEY);"})
        self.assertFalse(any("declares no primary key" in item for item in found))

    def test_a_virtual_table_is_not_reported_as_unkeyed(self) -> None:
        # Its module owns row identity, so "no primary key" would report a
        # property of fts5 as a gap in this repository's schema.
        found = self._claims({"store.sql": "CREATE VIRTUAL TABLE s USING fts5(a, b);"})
        self.assertFalse(any("declares no primary key" in item for item in found))

    def test_a_fixture_table_is_labelled_as_one(self) -> None:
        found = self._claims({"test_store.py": 'S = "CREATE TABLE t (a TEXT);"\n'})
        self.assertTrue(any("test fixture" in item for item in found))

    def test_an_example_schema_in_documentation_is_not_declared(self) -> None:
        # A README's illustrative table is how a repository acquires storage it
        # does not have.
        self.assertEqual(self._claims({"README.md": "```sql\nCREATE TABLE t (a TEXT);\n```\n"}), [])

    def test_a_shared_leading_column_is_reported_only_when_shared(self) -> None:
        agree = self._claims(
            {"s.sql": "CREATE INDEX i ON t (k, a);\nCREATE INDEX j ON t (k, b);\n"}
        )
        self.assertTrue(any("lead with column `k`" in item for item in agree))
        differ = self._claims(
            {"s.sql": "CREATE INDEX i ON t (k, a);\nCREATE INDEX j ON t (m, b);\n"}
        )
        self.assertFalse(any("lead with column" in item for item in differ))

    def test_a_single_index_states_no_pattern(self) -> None:
        # One index sharing a leading column with itself is not a pattern.
        found = self._claims({"s.sql": "CREATE INDEX i ON t (k, a);"})
        self.assertFalse(any("lead with column" in item for item in found))


class DifferentialTests(TestCase):
    """The oracle must actually run, and must disagree when the reader is wrong."""

    def test_the_reader_and_sqlite_agree_on_a_schema(self) -> None:
        from run_sql_differential import compare

        sql = (
            "CREATE TABLE t (id TEXT NOT NULL, a TEXT, PRIMARY KEY (id));\n"
            "CREATE INDEX i ON t (id, a);\n"
        )
        self.assertEqual(compare(sql), [])

    def test_ddl_without_a_terminator_is_still_executed(self) -> None:
        # DDL embedded in a string literal frequently carries no semicolon.
        # Scanning to the next `;` ran off the end of the literal, swallowed
        # the surrounding code, and reported every table as missing.
        from run_sql_differential import _executable_statements

        source = 'SCHEMA = """\nCREATE TABLE t (a TEXT)\n"""\nprint("x");\n'
        statements = _executable_statements(source)
        self.assertEqual(len(statements), 1)
        self.assertTrue(statements[0].endswith(");"))
