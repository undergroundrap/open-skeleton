# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import ast
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.python_ast import (
    PythonAstAnalyzer,
    _embedded_literals,
    _external_calls,
    _imported_names,
    _model_fields,
    _payload_shapes,
    _signatures,
    _string_constants,
)
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository

PYTHON_FIXTURE = """\
import os
import sqlite3

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"])
cache = {}
cache["boot"] = True
connection = sqlite3.connect("fixture.db")
connection.execute("CREATE TABLE IF NOT EXISTS widgets (data TEXT)")


@app.get("/health")
def health(limit: int = 1) -> dict[str, str | None]:
    return {"mode": os.getenv("APP_MODE")}


if __name__ == "__main__":
    print(health())
"""


class PythonAnalyzerTests(TestCase):
    def test_python_facts_have_receipts_and_calibrated_status(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(PYTHON_FIXTURE, encoding="utf-8")
            (root / "test_app.py").write_text(
                "def test_truth():\n    assert True\n",
                encoding="utf-8",
            )

            snapshot = scan_repository(root)
            result = analyze_snapshot(snapshot)
            claims = {item.claim: item for item in result.claims}

            self.assertEqual(result.coverage[0].coverage_ratio, 1.0)
            self.assertTrue(any(item.qualified_name.endswith(".health") for item in result.symbols))
            self.assertTrue(any(item.relationship == "imports" for item in result.edges))
            self.assertIn("GET /health is handled by app.health.", claims)
            self.assertIn("Python source declares 1 HTTP route handlers.", claims)
            self.assertIn("app.health reads environment setting APP_MODE.", claims)
            self.assertIn("app creates SQLite table widgets.", claims)
            self.assertIn(
                "app configures CORSMiddleware with a wildcard allow_origins value.",
                claims,
            )
            self.assertTrue(any(item.category == "auth_control_census" for item in result.claims))
            self.assertTrue(
                any(item.category == "http_framework_behavior" for item in result.claims)
            )

            state_claim = claims[
                "app.cache is a module-owned mutable container with observed mutation sites; "
                "its contents are process-local unless code outside this module synchronizes "
                "them to durable storage."
            ]
            self.assertEqual(state_claim.status, "inferred")
            self.assertTrue(state_claim.alternative_hypotheses)

            evidence_ids = {item.evidence_id for item in result.evidence}
            for claim in result.claims:
                self.assertTrue(set(claim.supporting_evidence).issubset(evidence_ids))
                if claim.status == "verified":
                    self.assertTrue(claim.supporting_evidence)

    def test_changed_file_after_snapshot_is_reported_as_coverage_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            source.write_text("value = 1\n", encoding="utf-8")
            snapshot = scan_repository(root)
            source.write_text("value = 2\n", encoding="utf-8")

            result = analyze_snapshot(snapshot)

            self.assertEqual(result.coverage[0].analyzed_files, 0)
            self.assertEqual(result.coverage[0].failed_files, 1)
            self.assertIn("content changed after snapshot", result.coverage[0].failures[0])

    def test_analysis_persists_and_is_searchable(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            (root / "app.py").write_text(PYTHON_FIXTURE, encoding="utf-8")
            snapshot = scan_repository(root)
            result = analyze_snapshot(snapshot)
            ledger = EvidenceLedger(workspace / "state" / "evidence.sqlite3")

            ledger.save_snapshot(snapshot)
            run_id = ledger.save_analysis(result)

            latest = ledger.latest_analysis(snapshot.snapshot_id)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest["run_id"], run_id)
            claims = ledger.list_claims(snapshot.snapshot_id, category="http_route")
            self.assertEqual(len(claims), 1)
            matches = ledger.search_claims(snapshot.snapshot_id, "SQLite")
            self.assertGreaterEqual(len(matches), 1)
            receipt = ledger.get_evidence(claims[0]["supporting_evidence"][0])
            self.assertIsNotNone(receipt)

    def test_unimported_sibling_is_only_an_orphan_candidate(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "app" / "core"
            package.mkdir(parents=True)
            (root / "main.py").write_text(
                "from app.core.used import value\nprint(value)\n", encoding="utf-8"
            )
            (package / "used.py").write_text("value = 1\n", encoding="utf-8")
            (package / "unused.py").write_text("value = 2\n", encoding="utf-8")

            result = analyze_snapshot(scan_repository(root))
            candidates = [item for item in result.claims if item.category == "orphan_candidate"]

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].status, "inferred")
            self.assertIn("not a deletion instruction", candidates[0].claim)

    def test_state_reconciliation_math_conflict_and_operator_harness(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "backend").mkdir()
            (root / "scripts").mkdir()
            (root / "backend" / "main.py").write_text(
                """\
PLAYER_GROWTH = 1.15
_runs = {}

def login(player):
    if player.active_run_id and player.active_run_id not in _runs:
        player.active_run_id = None

def scale(player):
    # The wall stays non-trivial forever, never trivial.
    difficulty = 1.10 ** player.ascension_count
    power = PLAYER_GROWTH ** player.ascension_count
    return power / difficulty
""",
                encoding="utf-8",
            )
            (root / "scripts" / "smoke.py").write_text(
                "import sys\nif __name__ == '__main__':\n    sys.exit(0)\n",
                encoding="utf-8",
            )

            result = analyze_snapshot(scan_repository(root))
            categories = {item.category: item for item in result.claims}

            self.assertIn("state_reconciliation", categories)
            self.assertIn("mathematical_conflict", categories)
            self.assertEqual(categories["mathematical_conflict"].status, "conflict")
            self.assertTrue(categories["mathematical_conflict"].contradicting_evidence)
            self.assertIn("process_termination", categories)
            self.assertEqual(categories["process_termination"].status, "verified")
            self.assertIn("operator_harness", categories)
            self.assertEqual(categories["operator_harness"].status, "inferred")

    def test_testing_census_preserves_nonstandard_harness_alternative(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")

            result = analyze_snapshot(scan_repository(root))
            census = next(item for item in result.claims if item.category == "testing_census")

            self.assertEqual(census.status, "verified")
            self.assertIn("may still exist", census.claim)
            self.assertTrue(census.alternative_hypotheses)

    def test_analysis_reports_progress_after_each_adapter(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            events: list[tuple[str, int, int]] = []

            result = analyze_snapshot(
                scan_repository(root), on_event=lambda *event: events.append(event)
            )

            # One event per registered adapter, asserted against the adapters
            # that actually reported coverage rather than a hardcoded count, so
            # adding an analyzer does not require editing this expectation.
            reporting_analyzers = {item.analyzer for item in result.coverage}
            self.assertEqual(len(events), len(reporting_analyzers))
            self.assertEqual({name for name, _, _ in events}, reporting_analyzers)
            self.assertTrue(events[0][0].startswith("python-ast/"))
            self.assertTrue(all(elapsed >= 0 for _, elapsed, _ in events))

    def test_ai_exception_fallback_preserves_none_and_empty_values(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                """\
async def describe(ai_client):
    try:
        return {"description": await ai_client.generate_content("prompt")}
    except Exception:
        return {"description": None, "summary": ""}
""",
                encoding="utf-8",
            )

            result = analyze_snapshot(scan_repository(root))
            claim = next(item for item in result.claims if item.category == "ai_failure_behavior")

            self.assertEqual(claim.status, "verified")
            self.assertIn("None", claim.claim)
            self.assertIn("empty string", claim.claim)


class ControlFlowExtractionTests(TestCase):
    SOURCE = """
from fastapi import FastAPI, HTTPException
app = FastAPI()

@app.post("/a/{pid}")
async def handler(pid: str):
    if not lookup(pid):
        raise HTTPException(status_code=404, detail="missing")
    def helper():
        if never_reached:
            return 1
    for item in items:
        if item.bad:
            continue
    return {"ok": True}
"""

    def _flow(self, source: str) -> list[dict[str, object]]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text(source, encoding="utf-8")
            result = PythonAstAnalyzer().analyze(scan_repository(root))
        for symbol in result.symbols:
            flow = symbol.metadata.get("control_flow")
            if flow:
                return list(flow)
        return []

    def test_guards_raises_and_returns_are_recorded_in_line_order(self) -> None:
        flow = self._flow(self.SOURCE)
        kinds = [item["kind"] for item in flow]
        self.assertEqual(kinds[0], "guard")
        self.assertEqual(kinds[1], "raise")
        self.assertEqual(flow[-1]["kind"], "return")
        self.assertEqual([item["line"] for item in flow], sorted(item["line"] for item in flow))

    def test_a_nested_function_body_is_not_entered(self) -> None:
        flow = self._flow(self.SOURCE)
        labels = [item["label"] for item in flow]
        self.assertNotIn("never_reached", labels)

    def test_http_status_is_extracted_from_a_literal(self) -> None:
        flow = self._flow(self.SOURCE)
        raises = [item for item in flow if item["kind"] == "raise"]
        self.assertEqual(raises[0]["label"], "HTTP 404")

    def test_a_positional_status_argument_is_also_read(self) -> None:
        flow = self._flow(
            "from fastapi import FastAPI, HTTPException\n"
            "app = FastAPI()\n"
            '@app.get("/x")\n'
            "def h():\n"
            "    if bad:\n"
            "        raise HTTPException(429)\n"
            "    return 1\n"
        )
        raises = [item for item in flow if item["kind"] == "raise"]
        self.assertEqual(raises[0]["label"], "HTTP 429")

    def test_non_route_functions_record_no_control_flow(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "m.py").write_text(
                "def plain():\n    if x:\n        return 1\n", encoding="utf-8"
            )
            result = PythonAstAnalyzer().analyze(scan_repository(root))
        self.assertTrue(all("control_flow" not in item.metadata for item in result.symbols))


class StateValueExtractionTests(TestCase):
    def _fields(self, source: str) -> dict[str, Any]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "m.py").write_text(source, encoding="utf-8")
            result = PythonAstAnalyzer().analyze(scan_repository(root))
        for symbol in result.symbols:
            fields = symbol.metadata.get("state_fields")
            if fields:
                return dict(fields)
        return {}

    def test_the_guard_recorded_is_the_real_enclosing_condition(self) -> None:
        # Real code gates a state write on a derived boolean, not on the field
        # itself. The recorded condition must be that boolean, verbatim.
        fields = self._fields(
            "def resolve(run, wiped, cleared):\n"
            "    if wiped:\n"
            "        run.status = 'wiped'\n"
            "    elif cleared:\n"
            "        run.status = 'cleared'\n"
        )
        entries = {(value, condition) for value, condition, _ in fields["status"]["entries"]}
        self.assertIn(("wiped", "wiped"), entries)
        self.assertIn(("cleared", "cleared"), entries)

    def test_an_unconditional_assignment_records_no_condition(self) -> None:
        fields = self._fields(
            "def start(run):\n    run.status = 'active'\n    run.status = 'idle'\n"
        )
        conditions = {condition for _, condition, _ in fields["status"]["entries"]}
        self.assertEqual(conditions, {""})

    def test_a_field_with_one_value_is_not_reported(self) -> None:
        self.assertEqual(self._fields("def f(x):\n    x.mode = 'only'\n"), {})

    def test_comparisons_contribute_values_but_need_an_assignment(self) -> None:
        # A field only ever compared is not a state this module writes.
        self.assertEqual(self._fields("def f(x):\n    if x.status == 'a':\n        pass\n"), {})

    def test_subscript_assignment_is_treated_as_a_field(self) -> None:
        fields = self._fields(
            "def f(d, ok):\n    if ok:\n        d['phase'] = 'open'\n    d['phase'] = 'closed'\n"
        )
        self.assertIn("phase", fields)
        self.assertEqual(len(fields["phase"]["values"]), 2)

    def test_every_entry_carries_its_line(self) -> None:
        fields = self._fields("def f(x, ok):\n    if ok:\n        x.s = 'a'\n    x.s = 'b'\n")
        lines = {line for _, _, line in fields["s"]["entries"]}
        self.assertEqual(lines, {3, 4})


class PayloadShapeTests(TestCase):
    """Where responses are dictionaries, the contract is only in the literals."""

    def _shapes(self, source: str) -> dict[str, Any]:
        return _payload_shapes(ast.parse(source))

    def test_returned_literal_keys_are_recorded(self) -> None:
        shapes = self._shapes("def act():\n    return {'success': True, 'gold': 5}\n")
        self.assertEqual(shapes["act"]["fields"], ["gold", "success"])

    def test_several_return_shapes_contribute_their_union(self) -> None:
        shapes = self._shapes(
            "def act(ok):\n"
            "    if ok:\n"
            "        return {'success': True}\n"
            "    return {'success': False, 'message': 'no'}\n"
        )
        self.assertEqual(shapes["act"]["fields"], ["message", "success"])

    def test_a_conditional_expression_contributes_both_branches(self) -> None:
        shapes = self._shapes("def act(ok):\n    return {'a': 1} if ok else {'b': 2}\n")
        self.assertEqual(shapes["act"]["fields"], ["a", "b"])

    def test_a_computed_key_is_absent_rather_than_guessed(self) -> None:
        shapes = self._shapes("def act(name):\n    return {name: 1, 'fixed': 2}\n")
        self.assertEqual(shapes["act"]["fields"], ["fixed"])

    def test_a_function_returning_no_dictionary_has_no_shape(self) -> None:
        self.assertEqual(self._shapes("def act():\n    return 5\n"), {})


class ModelFieldTests(TestCase):
    """Where a repository persists JSON, annotated classes are the whole schema."""

    def _models(self, source: str) -> dict[str, Any]:
        return _model_fields(ast.parse(source))

    def test_annotated_fields_are_recorded_with_their_base(self) -> None:
        models = self._models(
            "class Player(BaseModel):\n    gold: int = 0\n    name: str\n",
        )
        self.assertEqual(models["Player"]["bases"], ["BaseModel"])
        names = [item["name"] for item in models["Player"]["fields"]]
        self.assertEqual(names, ["gold", "name"])

    def test_a_field_without_a_default_is_required_and_one_with_a_default_is_not(self) -> None:
        fields = {
            item["name"]: item
            for item in self._models("class P:\n    a: int\n    b: int = 3\n")["P"]["fields"]
        }
        self.assertTrue(fields["a"]["required"])
        self.assertFalse(fields["b"]["required"])
        self.assertEqual(fields["b"]["default"], "3")

    def test_the_annotation_is_kept_as_written(self) -> None:
        models = self._models("class P:\n    ids: List[str] = []\n")
        self.assertEqual(models["P"]["fields"][0]["annotation"], "List[str]")

    def test_an_unannotated_class_attribute_is_not_a_declared_field(self) -> None:
        self.assertEqual(self._models("class P:\n    total = 3\n"), {})


class ImportedNameTests(TestCase):
    def _imports(self, source: str) -> dict[str, Any]:
        return _imported_names(ast.parse(source))

    def test_the_names_a_module_contributes_are_recorded(self) -> None:
        imports = self._imports("from fastapi import Depends, FastAPI\n")
        self.assertEqual(imports["fastapi"]["names"], ["Depends", "FastAPI"])

    def test_an_alias_is_recorded_under_the_local_name(self) -> None:
        imports = self._imports("from numpy import array as arr\n")
        self.assertEqual(imports["numpy"]["names"], ["arr"])

    def test_a_relative_import_keeps_its_dots_as_the_target(self) -> None:
        self.assertIn(".core", self._imports("from .core import engine\n"))

    def test_names_from_repeated_imports_of_one_module_are_merged(self) -> None:
        imports = self._imports("from typing import Any\nfrom typing import Dict\n")
        self.assertEqual(imports["typing"]["names"], ["Any", "Dict"])


class StringConstantTests(TestCase):
    def _constants(self, source: str) -> dict[str, Any]:
        return _string_constants(ast.parse(source))

    def test_a_named_string_is_recorded_with_its_value(self) -> None:
        constants = self._constants('_TELEGRAPH = "ANNIHILATE"\n')
        self.assertEqual(constants["_TELEGRAPH"]["value"], "ANNIHILATE")

    def test_a_module_docstring_is_not_a_constant(self) -> None:
        self.assertEqual(self._constants('"""Module prose."""\n'), {})

    def test_a_computed_value_is_not_recorded(self) -> None:
        self.assertEqual(self._constants('NAME = "a" + "b"\n'), {})

    def test_an_annotated_assignment_is_recorded(self) -> None:
        self.assertEqual(self._constants('NAME: str = "x"\n')["NAME"]["value"], "x")


class EmbeddedLiteralTests(TestCase):
    """A number written into logic is a decision made without a name."""

    def _literals(self, source: str) -> dict[str, Any]:
        return _embedded_literals(ast.parse(source))

    def test_literals_in_a_body_are_recorded_with_their_lines(self) -> None:
        found = self._literals("def f():\n    wait = 300.0\n    tries = 5\n")
        values = {item["value"]: item["line"] for item in found["f"]["values"]}
        self.assertEqual(values, {"300.0": 2, "5": 3})

    def test_zero_and_one_are_excluded_as_structural(self) -> None:
        self.assertEqual(self._literals("def f():\n    return [0, 1]\n"), {})

    def test_a_negative_literal_keeps_its_sign(self) -> None:
        found = self._literals("def f():\n    return -20\n")
        self.assertEqual(found["f"]["values"][0]["value"], "-20")

    def test_a_repeated_value_is_counted_once_at_its_first_site(self) -> None:
        found = self._literals("def f():\n    a = 7\n    b = 7\n")
        self.assertEqual(found["f"]["values"], [{"value": "7", "line": 2}])

    def test_a_nested_function_owns_its_own_literals(self) -> None:
        found = self._literals("def outer():\n    def inner():\n        return 42\n    return 3\n")
        self.assertEqual([item["value"] for item in found["outer"]["values"]], ["3"])
        self.assertEqual([item["value"] for item in found["inner"]["values"]], ["42"])


class SignatureTests(TestCase):
    """A caller needs the shape of the call, not only the name."""

    def _sig(self, source: str) -> dict[str, Any]:
        return _signatures(ast.parse(source))

    def test_annotations_defaults_and_return_are_recorded(self) -> None:
        entry = self._sig('def f(a: int, b: str = "x") -> dict:\n    return {}\n')["f"]
        self.assertEqual(entry["returns"], "dict")
        self.assertEqual(
            entry["parameters"][0], {"name": "a", "kind": "positional", "annotation": "int"}
        )
        self.assertEqual(entry["parameters"][1]["default"], "'x'")

    def test_defaults_bind_to_the_tail_of_the_positional_list(self) -> None:
        parameters = self._sig("def f(a, b, c=3):\n    pass\n")["f"]["parameters"]
        self.assertNotIn("default", parameters[0])
        self.assertNotIn("default", parameters[1])
        self.assertEqual(parameters[2]["default"], "3")

    def test_var_positional_and_var_keyword_keep_their_kinds(self) -> None:
        kinds = {
            p["name"]: p["kind"]
            for p in self._sig("def f(*args, **kw):\n    pass\n")["f"]["parameters"]
        }
        self.assertEqual(kinds, {"args": "var_positional", "kw": "var_keyword"})

    def test_a_keyword_only_parameter_without_a_default_has_none(self) -> None:
        parameters = self._sig("def f(*, mode):\n    pass\n")["f"]["parameters"]
        self.assertEqual(parameters[0]["kind"], "keyword_only")
        self.assertNotIn("default", parameters[0])

    def test_a_mutable_default_stays_visible_as_an_expression(self) -> None:
        # Evaluating it would hide the bug; the expression is the finding.
        self.assertEqual(
            self._sig("def f(items=[]):\n    pass\n")["f"]["parameters"][0]["default"], "[]"
        )


class SchemaMigrationTests(TestCase):
    """A schema evolved in code still evolves, and f-strings are how it is written."""

    def _claims(self, source: str) -> list[Any]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "store.py").write_text(source, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
        return [item for item in result.claims if item.category == "schema_migration"]

    def test_a_literal_alter_names_the_table_and_column(self) -> None:
        claims = self._claims(
            "import sqlite3\n"
            "c = sqlite3.connect('x.db')\n"
            "c.execute('ALTER TABLE players ADD COLUMN gold INTEGER')\n"
        )
        self.assertEqual(len(claims), 1)
        self.assertIn("SQLite table players", claims[0].claim)
        self.assertIn("column gold", claims[0].claim)

    def test_an_interpolated_alter_says_the_names_are_unknown(self) -> None:
        # Migrations are routinely built with an f-string. Reporting only
        # literals made every such project look unable to migrate at all.
        claims = self._claims(
            "import sqlite3\n"
            "c = sqlite3.connect('x.db')\n"
            "def go(table, column, definition):\n"
            "    c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')\n"
        )
        self.assertEqual(len(claims), 1)
        self.assertIn("named at runtime", claims[0].claim)

    def test_a_create_table_is_not_a_migration(self) -> None:
        claims = self._claims(
            "import sqlite3\n"
            "c = sqlite3.connect('x.db')\n"
            "c.execute('CREATE TABLE IF NOT EXISTS players (id TEXT)')\n"
        )
        self.assertEqual(claims, [])


class ExternalCallTests(TestCase):
    """An import edge names a module; this names what is called through it."""

    def _calls(self, source: str) -> dict[str, Any]:
        return _external_calls(ast.parse(source))

    def test_a_call_through_an_imported_module_is_recorded(self) -> None:
        calls = self._calls("import os\nos.path.join(a, b)\n")
        self.assertEqual(calls["os.path.join"]["via"], "os")

    def test_self_and_parameters_are_this_module_s_own_wiring(self) -> None:
        source = "class E:\n    def go(self, mob):\n        self.helper()\n        mob.roll()\n"
        self.assertEqual(self._calls(source), {})

    def test_a_client_built_from_an_import_counts_as_that_import(self) -> None:
        calls = self._calls(
            "from openai import AsyncOpenAI\nclient = AsyncOpenAI()\nclient.chat.create(x=1)\n"
        )
        self.assertEqual(calls["client.chat.create"]["via"], "AsyncOpenAI")

    def test_an_alias_resolves_to_the_imported_name(self) -> None:
        calls = self._calls("import numpy as np\nnp.array([1])\n")
        self.assertEqual(calls["np.array"]["via"], "numpy")

    def test_repeated_sites_are_counted_and_located_at_the_first(self) -> None:
        calls = self._calls("import time\ntime.time()\ntime.time()\n")
        self.assertEqual(calls["time.time"]["count"], 2)
        self.assertEqual(calls["time.time"]["first_line"], 2)


class RouteRoleTests(TestCase):
    """A route registered inside a test is not part of the served surface."""

    def _claims(self, filename: str) -> dict[str, list[str]]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / filename).write_text(
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/health')\n"
                "def health():\n"
                "    return {'ok': True}\n",
                encoding="utf-8",
            )
            result = analyze_snapshot(scan_repository(root))
        grouped: dict[str, list[str]] = {}
        for item in result.claims:
            grouped.setdefault(item.category, []).append(item.claim)
        return grouped

    def test_a_route_in_application_code_is_served(self) -> None:
        grouped = self._claims("app.py")
        self.assertIn("http_route", grouped)
        self.assertNotIn("test_route", grouped)

    def test_a_route_in_a_test_file_is_categorised_separately(self) -> None:
        # Flask's own suite registers sixteen routes inside tests. Reporting
        # them as endpoints misdescribes what the system exposes.
        grouped = self._claims("test_app.py")
        self.assertIn("test_route", grouped)
        self.assertNotIn("http_route", grouped)
        self.assertIn("exercises the framework", grouped["test_route"][0])


class EndpointPositionTests(TestCase):
    """A URL-shaped string is only an endpoint if the program dials it.

    Found by running this analyzer on its own repository. It reported five
    hardcoded network endpoints in a tool whose design guarantee is that it
    makes no network calls at all. Every one was a string in a position that
    means something else, including the detector literal the TypeScript
    analyzer searches for -- the analyzer reporting its own search term as an
    address it contacts.
    """

    def _endpoints(self, source: str, role: str = "source") -> set[str]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            name = "test_sample.py" if role == "test" else "sample.py"
            (root / name).write_text(source, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
        found: set[str] = set()
        for claim in result.claims:
            if claim.category == "hardcoded_endpoint":
                found.add(claim.claim)
        return found

    def test_a_dialled_endpoint_is_still_reported(self) -> None:
        found = self._endpoints('import x\nURL = "https://api.example.com/v1"\nx.get(URL)\n')
        self.assertTrue(found, "a genuine hardcoded endpoint must still be reported")

    def test_a_searched_for_literal_is_not_an_endpoint(self) -> None:
        # The exact shape that produced the false claim: a detector string.
        self.assertEqual(
            self._endpoints('def f(t):\n    return "http://localhost:8000" in t\n'), set()
        )

    def test_a_match_method_argument_is_not_an_endpoint(self) -> None:
        self.assertEqual(
            self._endpoints('def f(t):\n    return t.count("http://a.test/b")\n'), set()
        )

    def test_a_schema_identifier_is_not_an_endpoint(self) -> None:
        # `$schema` names a vocabulary. Nothing fetches it.
        self.assertEqual(
            self._endpoints('S = {"$schema": "https://json-schema.org/draft/2020-12/schema"}\n'),
            set(),
        )

    def test_a_fixture_endpoint_is_not_the_systems_endpoint(self) -> None:
        source = 'import x\ndef test_it():\n    x.get("https://fixture.test/a")\n'
        self.assertEqual(self._endpoints(source, role="test"), set())


class AnalyzerContractTests(TestCase):
    """Every analyzer satisfies the protocol that describes one.

    `Analyzer` was flagged as an orphan by this tool running on its own
    repository: nothing imported it, so a Protocol meant to constrain five
    implementations constrained none of them. Structural typing does not
    require the import, which is exactly why an unreferenced Protocol is
    documentation wearing a type's clothes. Asserting conformance here is what
    turns it back into a contract.
    """

    def test_every_registered_analyzer_satisfies_the_protocol(self) -> None:
        from open_skeleton.analysis import build_analyzers
        from open_skeleton.analyzers.base import Analyzer

        registry = build_analyzers()
        self.assertTrue(registry, "the registry must not be empty")
        for analyzer in registry:
            checked: Analyzer = analyzer
            self.assertTrue(checked.name, f"{analyzer!r} declares no name")
            self.assertTrue(checked.version, f"{analyzer!r} declares no version")
