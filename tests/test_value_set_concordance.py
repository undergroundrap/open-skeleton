# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Closed vocabularies, recovered from each form and joined across them.

This repository declares the claim-status vocabulary five times: a SQL CHECK,
a schema enum, a CLI `choices`, a `Literal` annotation, and a runtime guard.
Adding a member means changing all five, and until this join existed nothing
said where they were. That is the whole point -- a specification that saves a
reader from crawling the repository.

The join key is the member set, never the name. These tests hold that line
from both directions: one vocabulary under two different names must join, and
two vocabularies under the same name must not.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analyzers.project_metadata import _schema_value_sets
from open_skeleton.analyzers.python_ast import _declared_value_sets
from open_skeleton.analyzers.sql_schema import parse_tables
from open_skeleton.spec.concordance import build_value_set_concordance


def _symbol(path: str, *sets: dict[str, Any]) -> dict[str, Any]:
    return {"path": path, "metadata": {"value_sets": list(sets)}}


def _entry(label: str, members: list[str], kind: str, line: int = 1) -> dict[str, Any]:
    return {"label": label, "members": members, "kind": kind, "line": line}


class PythonExtractionTests(TestCase):
    def _sets(self, source: str) -> dict[str, list[str]]:
        return {
            f"{item['kind']}:{item['label']}": item["members"]
            for item in _declared_value_sets(ast.parse(source))
        }

    def test_a_literal_annotation_is_a_vocabulary(self) -> None:
        found = self._sets("def go(status: Literal['open', 'closed']) -> None: ...")
        self.assertEqual(found["literal_type:status"], ["closed", "open"])

    def test_a_literal_survives_an_optional_union(self) -> None:
        found = self._sets("def go(status: Literal['open', 'closed'] | None = None): ...")
        self.assertEqual(found["literal_type:status"], ["closed", "open"])

    def test_a_string_enum_class_reports_its_values(self) -> None:
        found = self._sets("class Quest(str, Enum):\n    KILL = 'kill'\n    HUNT = 'hunt'\n")
        self.assertEqual(found["enum_class:Quest"], ["hunt", "kill"])

    def test_argparse_choices_are_a_vocabulary(self) -> None:
        found = self._sets("parser.add_argument('--mode', choices=['fast', 'slow'])")
        self.assertEqual(found["cli_choices:mode"], ["fast", "slow"])

    def test_a_membership_guard_is_a_vocabulary(self) -> None:
        found = self._sets("if self.status not in {'a', 'b'}:\n    raise ValueError('x')\n")
        self.assertEqual(found["membership_guard:status"], ["a", "b"])

    def test_a_collection_holding_a_non_literal_is_not_closed(self) -> None:
        # One computed element means the set of accepted values is not written
        # down, and reporting the literal part would name a set nothing uses.
        self.assertEqual(self._sets("if x in {'a', compute()}:\n    pass\n"), {})

    def test_a_single_member_is_not_a_vocabulary(self) -> None:
        self.assertEqual(self._sets("if x in {'only'}:\n    pass\n"), {})


class SqlAndSchemaExtractionTests(TestCase):
    def test_a_check_constraint_names_its_column_vocabulary(self) -> None:
        tables = parse_tables(
            "CREATE TABLE claims (status TEXT NOT NULL CHECK (status IN ('verified', 'stale')));"
        )
        column = next(item for item in tables[0].columns if item.name == "status")
        self.assertEqual(column.allowed_values, ("stale", "verified"))

    def test_a_check_on_another_column_is_not_this_column_vocabulary(self) -> None:
        # A table-level CHECK naming a different column would otherwise be
        # attributed to whichever column the parser happened to be reading.
        tables = parse_tables("CREATE TABLE t (a TEXT CHECK (b IN ('x', 'y')), b TEXT);")
        first = next(item for item in tables[0].columns if item.name == "a")
        self.assertEqual(first.allowed_values, ())

    def test_a_numeric_range_check_declares_no_vocabulary(self) -> None:
        tables = parse_tables("CREATE TABLE t (c REAL CHECK (c >= 0.0 AND c <= 1.0));")
        self.assertEqual(tables[0].columns[0].allowed_values, ())

    def test_a_schema_enum_is_read_with_its_property_name(self) -> None:
        document = {"properties": {"status": {"enum": ["verified", "stale"]}}}
        found = _schema_value_sets(document)
        self.assertEqual(found[0]["label"], "status")
        self.assertEqual(found[0]["members"], ["stale", "verified"])

    def test_a_non_string_enum_is_not_a_vocabulary(self) -> None:
        self.assertEqual(_schema_value_sets({"properties": {"n": {"enum": [1, 2]}}}), [])


class ValueSetJoinTests(TestCase):
    def _join(self, *symbols: dict[str, Any]) -> tuple[Any, tuple[str, ...]]:
        return build_value_set_concordance(snapshot_id="s", symbols=tuple(symbols))

    def test_one_vocabulary_in_two_forms_joins(self) -> None:
        joined, _ = self._join(
            _symbol("db.py", _entry("status", ["a", "b"], "sql_check")),
            _symbol("models.py", _entry("status", ["a", "b"], "membership_guard")),
        )
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0].members, ("a", "b"))
        self.assertEqual(joined[0].kinds, ("membership_guard", "sql_check"))

    def test_the_same_vocabulary_under_different_names_still_joins(self) -> None:
        # The real case: `{dungeon, raid}` is a guard called `preset` in one
        # file and a CLI `choices` called `target` in another.
        joined, _ = self._join(
            _symbol("main.py", _entry("preset", ["dungeon", "raid"], "membership_guard")),
            _symbol("cli.py", _entry("target", ["dungeon", "raid"], "cli_choices")),
        )
        self.assertEqual(len(joined), 1)

    def test_two_vocabularies_under_the_same_name_do_not_join(self) -> None:
        # This repository spells three different vocabularies `status`.
        # Joining on the name would merge them into one false contract.
        joined, ambiguous = self._join(
            _symbol("a.py", _entry("status", ["verified", "stale"], "sql_check")),
            _symbol("b.py", _entry("status", ["complete", "disabled"], "membership_guard")),
        )
        self.assertEqual(joined, ())
        self.assertIn("status", ambiguous)

    def test_one_form_repeated_is_not_a_contract(self) -> None:
        # Two `in {...}` guards inside one tokenizer are the same form used
        # twice. This repository tests `{"(", "[", "{"}` in six files.
        joined, _ = self._join(
            _symbol("lex_a.py", _entry("value", ["(", "["], "membership_guard")),
            _symbol("lex_b.py", _entry("value", ["(", "["], "membership_guard")),
        )
        self.assertEqual(joined, ())

    def test_one_site_alone_is_not_a_contract(self) -> None:
        joined, _ = self._join(_symbol("only.py", _entry("s", ["a", "b"], "sql_check")))
        self.assertEqual(joined, ())

    def test_an_incomplete_overlap_is_not_a_join(self) -> None:
        # A subset is a different vocabulary, and calling it the same one is
        # exactly the divergence this reader must not silently resolve.
        joined, _ = self._join(
            _symbol("a.py", _entry("s", ["a", "b", "c"], "sql_check")),
            _symbol("b.py", _entry("s", ["a", "b"], "schema_enum")),
        )
        self.assertEqual(joined, ())

    def test_member_order_and_duplicates_do_not_change_identity(self) -> None:
        joined, _ = self._join(
            _symbol("a.py", _entry("s", ["b", "a", "a"], "sql_check")),
            _symbol("b.py", _entry("s", ["a", "b"], "schema_enum")),
        )
        self.assertEqual(len(joined), 1)

    def test_a_serialization_suffix_does_not_make_a_label_ambiguous(self) -> None:
        # `failures_json` persists `failures`; that rename is systematic and is
        # the only one normalized.
        _, ambiguous = self._join(
            _symbol("a.py", _entry("failures_json", ["x", "y"], "sql_check")),
            _symbol("b.py", _entry("failures", ["x", "y"], "membership_guard")),
        )
        self.assertEqual(ambiguous, ())


class EndToEndTests(TestCase):
    def test_a_vocabulary_written_three_ways_reaches_the_document(self) -> None:
        from open_skeleton.analysis import analyze_snapshot
        from open_skeleton.scanner import scan_repository

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "store.py").write_text(
                "SCHEMA = '''\nCREATE TABLE job (\n"
                "  state TEXT NOT NULL CHECK (state IN ('queued', 'done'))\n);\n'''\n",
                encoding="utf-8",
            )
            (root / "guard.py").write_text(
                "def check(state):\n"
                "    if state not in {'queued', 'done'}:\n"
                "        raise ValueError(state)\n",
                encoding="utf-8",
            )
            (root / "job.schema.json").write_text(
                json.dumps({"properties": {"state": {"enum": ["queued", "done"]}}}),
                encoding="utf-8",
            )
            result = analyze_snapshot(scan_repository(root))
            symbols = tuple(
                {"path": item.path, "metadata": item.metadata} for item in result.symbols
            )
            joined, _ = build_value_set_concordance(snapshot_id="s", symbols=symbols)

            self.assertEqual(len(joined), 1)
            self.assertEqual(joined[0].members, ("done", "queued"))
            self.assertEqual(joined[0].kinds, ("membership_guard", "schema_enum", "sql_check"))
            self.assertEqual(len(joined[0].declarations), 3)
