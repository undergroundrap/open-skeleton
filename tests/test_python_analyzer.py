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
    _caught_families,
    _control_flow,
    _declared_cli_flags,
    _defined_exceptions,
    _embedded_literals,
    _external_calls,
    _imported_names,
    _model_fields,
    _module_name,
    _module_names,
    _name_index,
    _package_directories,
    _payload_shapes,
    _raise_message,
    _signatures,
    _string_constants,
)
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.models import AnalysisResult
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

    def _suite(self, sources: dict[str, str]) -> list[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            for name, body in sources.items():
                (root / "tests" / name).write_text(body, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            return [item.claim for item in result.claims if "helper(s) defined" in item.claim]

    def test_a_builder_several_suites_share_is_named(self) -> None:
        # "Where does the test data come from" is among the first questions
        # asked of an unfamiliar repository. Both halves of the answer were in
        # the ledger -- the call edges and the symbols -- and nothing put them
        # together.
        found = self._suite(
            {
                "helpers.py": "def build_repo(root):\n    return root\n",
                "test_a.py": "from tests.helpers import build_repo\n\ndef test_a():\n    build_repo(1)\n",
                "test_b.py": "from tests.helpers import build_repo\n\ndef test_b():\n    build_repo(2)\n",
            }
        )
        self.assertTrue(found)
        self.assertIn("`build_repo`", found[0])
        self.assertIn("2 module(s)", found[0])

    def test_a_helper_only_one_suite_uses_is_its_own_setup(self) -> None:
        found = self._suite(
            {
                "helpers.py": "def build_repo(root):\n    return root\n",
                "test_a.py": "from tests.helpers import build_repo\n\ndef test_a():\n    build_repo(1)\n",
            }
        )
        self.assertEqual(found, [])

    def test_a_name_each_suite_declares_for_itself_is_not_shared(self) -> None:
        # Five suites each defining their own `_claim` are not five callers of
        # one shared thing, and reporting them as such invents infrastructure
        # the repository does not have.
        found = self._suite(
            {
                "test_a.py": "def _claim(x):\n    return x\n\ndef test_a():\n    _claim(1)\n",
                "test_b.py": "def _claim(x):\n    return x\n\ndef test_b():\n    _claim(2)\n",
            }
        )
        self.assertEqual(found, [])

    def test_an_assertion_method_is_not_a_fixture_builder(self) -> None:
        # Without resolving callees against symbols defined in the suite, the
        # most-called names in any test tree are the assertion methods, which
        # say nothing about the repository.
        found = self._suite(
            {
                "test_a.py": "def test_a():\n    assert len([1]) == 1\n",
                "test_b.py": "def test_b():\n    assert len([2]) == 1\n",
            }
        )
        self.assertEqual(found, [])

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
import openai

async def describe():
    try:
        return {"description": await openai.generate("prompt")}
    except Exception:
        return {"description": None, "summary": ""}
""",
                encoding="utf-8",
            )

            result = analyze_snapshot(scan_repository(root))
            claim = next(item for item in result.claims if item.category == "absorbed_failure")

            self.assertEqual(claim.status, "verified")
            self.assertIn("None", claim.claim)
            self.assertIn("empty string", claim.claim)
            self.assertIn("`openai.generate`", claim.claim)


class AbsorbedFailureTests(TestCase):
    """A failure swallowed into a silent value, whatever was called.

    This required the name `ai_client` or a method called `generate_content`
    -- spellings from the repository it was written against -- so a handler
    wrapping `requests` or `redis` and returning None was invisible everywhere
    else. The generalization benchmark reported the category for one
    repository out of eight, which is how it was found.
    """

    def _absorbed(self, source: str) -> list[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "m.py").write_text(source, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            return [item.claim for item in result.claims if item.category == "absorbed_failure"]

    def test_any_imported_call_counts(self) -> None:
        for module, call in (("requests", "requests.get"), ("redis", "redis.get")):
            source = (
                f"import {module}\n\n"
                f"def read():\n    try:\n        return {call}('k')\n"
                "    except Exception:\n        return None\n"
            )
            found = self._absorbed(source)
            self.assertTrue(found, f"{call} should be reported")
            self.assertIn(f"`{call}`", found[0])

    def test_a_local_helper_is_ordinary_control_flow(self) -> None:
        # `try: helper()` returning None is how programs are written. Only a
        # call that crosses a boundary makes the silence worth reporting.
        source = (
            "def helper():\n    return 1\n\n"
            "def read():\n    try:\n        return helper()\n"
            "    except Exception:\n        return None\n"
        )
        self.assertEqual(self._absorbed(source), [])

    def test_a_handler_that_reraises_absorbs_nothing(self) -> None:
        source = (
            "import requests\n\n"
            "def read():\n    try:\n        return requests.get('k')\n"
            "    except Exception:\n        raise\n"
        )
        self.assertEqual(self._absorbed(source), [])


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


class ExponentialScalingTests(TestCase):
    """Growth reported by shape, not by the vocabulary of one repository.

    This fired only when the exponent expression contained the word
    "ascension", a term from the game the analyzer was first written against.
    The generalization benchmark exists to catch exactly that: the category
    appeared for one repository out of eight, which it flags as either a rare
    property or an analyzer fitted to one codebase. It was the second.
    """

    def _scaling(self, source: str) -> list[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(source, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            return [item.claim for item in result.claims if item.category == "exponential_scaling"]

    def test_growth_by_any_named_value_is_reported(self) -> None:
        for source, token in (
            ("def f(level):\n    return 1.15 ** level\n", "level"),
            ("def f(retries):\n    return 2 ** retries\n", "retries"),
            ("def f(tier):\n    return 1.5 ** tier\n", "tier"),
        ):
            found = self._scaling(source)
            self.assertTrue(found, f"{token} should be reported as a growth curve")
            self.assertIn(token, found[0])

    def test_a_constant_power_is_arithmetic_rather_than_growth(self) -> None:
        # `10 ** 6` is a number. Nothing about it varies at runtime, so there
        # is no curve to report.
        self.assertEqual(self._scaling("def f():\n    return 10 ** 6\n"), [])

    def _divergence(self, source: str) -> list[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "m.py").write_text(source, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            return [
                item.claim for item in result.claims if item.category == "mathematical_conflict"
            ]

    def test_two_bases_on_one_quantity_diverge(self) -> None:
        source = "def a(level):\n    return 1.25 ** level\n\ndef b(level):\n    return 2 ** level\n"
        found = self._divergence(source)
        self.assertTrue(found)
        self.assertIn("`level`", found[0])

    def test_the_same_quantity_spelled_differently_still_pairs(self) -> None:
        # `player.ascension_count`, `args.ascensions` and `ascensions` are one
        # quantity written three ways. Comparing the expressions literally
        # finds no pair and loses the finding this check exists for.
        source = (
            "def a(player):\n    return 1.1 ** player.ascension_count\n\n"
            "def b(ascensions):\n    return 1.15 ** ascensions\n"
        )
        self.assertTrue(self._divergence(source))

    def test_unrelated_curves_are_not_compared(self) -> None:
        # `1.25 ** tier` and `2 ** retries` are different curves. Reporting a
        # ratio between them would describe something nothing computes.
        source = (
            "def a(tier):\n    return 1.25 ** tier\n\ndef b(retries):\n    return 2 ** retries\n"
        )
        self.assertEqual(self._divergence(source), [])

    def test_a_shared_generic_word_is_not_a_shared_quantity(self) -> None:
        # `retry_count` and `user_count` share only `count`, which carries no
        # subject of its own.
        source = (
            "def a(retry_count):\n    return 2 ** retry_count\n\n"
            "def b(user_count):\n    return 3 ** user_count\n"
        )
        self.assertEqual(self._divergence(source), [])

    def test_a_divergence_nothing_contradicts_is_not_a_conflict(self) -> None:
        # A conflict needs two sides. Without a documented assertion this is an
        # unchallenged property of the arithmetic.
        source = "def a(level):\n    return 1.25 ** level\n\ndef b(level):\n    return 2 ** level\n"
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "m.py").write_text(source, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
        claim = next(item for item in result.claims if item.category == "mathematical_conflict")
        self.assertEqual(claim.status, "verified")
        self.assertTrue(claim.invalidation_keys, "a claim nothing can retire is a claim forever")

    def test_the_original_domain_case_still_fires(self) -> None:
        # The word is no longer required, but removing the requirement must
        # not stop it matching where it always did.
        found = self._scaling("def f(player):\n    return 1.10 ** player.ascension_count\n")
        self.assertTrue(found)
        self.assertIn("ascension_count", found[0])


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

    def test_a_client_held_on_an_attribute_is_followed_too(self) -> None:
        # The shape an SDK client actually takes. Binding only bare names meant
        # the call that follows is rooted at `self`, which is never an import,
        # so the single operation an external service contract consists of --
        # `chat.completions.create` -- was parsed into the call graph and then
        # dropped from the runtime surface.
        source = (
            "import openai\n"
            "class Client:\n"
            "    def __init__(self):\n"
            "        self.client = openai.AsyncOpenAI(base_url='x')\n"
            "    async def ask(self):\n"
            "        return await self.client.chat.completions.create(model='m')\n"
        )
        calls = self._calls(source)
        self.assertEqual(calls["self.client.chat.completions.create"]["via"], "openai")

    def test_a_bare_self_call_is_still_this_module_s_own_wiring(self) -> None:
        # Only an attribute *bound from an import* resolves. `self.helper()`
        # must stay excluded, or every method call in the repository becomes an
        # external dependency.
        source = (
            "import openai\n"
            "class Client:\n"
            "    def __init__(self):\n"
            "        self.client = openai.AsyncOpenAI()\n"
            "    def go(self):\n"
            "        self.helper()\n"
            "        self.other.thing()\n"
        )
        calls = self._calls(source)
        self.assertNotIn("self.helper", calls)
        self.assertNotIn("self.other.thing", calls)

    def test_the_longest_bound_prefix_wins(self) -> None:
        # Two clients on one object must not collapse into whichever was seen
        # first.
        source = (
            "import openai\nimport redis\n"
            "class C:\n"
            "    def __init__(self):\n"
            "        self.ai = openai.AsyncOpenAI()\n"
            "        self.cache = redis.Redis()\n"
            "    def go(self):\n"
            "        self.ai.chat.create()\n"
            "        self.cache.get('k')\n"
        )
        calls = self._calls(source)
        self.assertEqual(calls["self.ai.chat.create"]["via"], "openai")
        self.assertEqual(calls["self.cache.get"]["via"], "redis")

    def test_the_standard_library_is_marked_as_such(self) -> None:
        # Ranked purely by how often each name is called, `random.choice` at
        # forty-four sites outranked the two calls reaching a language model,
        # so a panel headed "what does this program touch" led with `time.time`.
        calls = self._calls("import time\ntime.time()\n")
        self.assertEqual(calls["time.time"]["origin"], "standard library")

    def test_the_repository_s_own_module_is_not_a_dependency(self) -> None:
        # `from app.core.vector_db import vec_db` binds an *object*, so the
        # module it lives in is not recoverable from the bound name. Without
        # tracking it separately, a repository's own storage layer was reported
        # as something the program depends on.
        calls = _external_calls(
            ast.parse("from app.core.vector_db import vec_db\nvec_db.get_player(1)\n"),
            frozenset({"backend.app.core.vector_db", "backend.main"}),
        )
        self.assertEqual(calls["vec_db.get_player"]["origin"], "this repository")

    def test_an_unrecognised_module_is_reported_as_a_dependency(self) -> None:
        # The honest residual: neither the standard library nor this
        # repository. It does not claim a manifest declares it.
        calls = _external_calls(
            ast.parse("import openai\nopenai.AsyncOpenAI()\n"), frozenset({"backend.main"})
        )
        self.assertEqual(calls["openai.AsyncOpenAI"]["origin"], "dependency")

    def test_via_still_names_what_it_always_named(self) -> None:
        # The module is tracked alongside `via`, not instead of it; changing
        # what `via` means would silently rewrite every existing row.
        calls = self._calls(
            "from openai import AsyncOpenAI\nclient = AsyncOpenAI()\nclient.chat.create()\n"
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


class StandardLibraryRouteTests(TestCase):
    """``http.server`` dispatch is a route declaration written as control flow."""

    SOURCE = (
        "from http.server import BaseHTTPRequestHandler\n"
        "from urllib.parse import urlparse\n\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        parsed = urlparse(self.path)\n"
        "        if parsed.path == '/':\n"
        "            return self.home()\n"
        "        if parsed.path.startswith('/api/evidence/'):\n"
        "            return self.evidence()\n"
    )

    def _result(self, source: str, filename: str = "server.py") -> Any:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / filename).write_text(source, encoding="utf-8")
            return analyze_snapshot(scan_repository(root))

    def test_literal_dispatch_in_a_standard_handler_becomes_routes(self) -> None:
        result = self._result(self.SOURCE)
        routes = sorted(claim.claim for claim in result.claims if claim.category == "http_route")

        self.assertEqual(len(routes), 2)
        self.assertTrue(any("GET / is handled by" in claim for claim in routes))
        self.assertTrue(
            any("GET /api/evidence/{remainder} is handled by" in claim for claim in routes)
        )
        inventory = [
            claim.claim for claim in result.claims if claim.category == "http_route_inventory"
        ]
        self.assertEqual(inventory, ["Python source declares 2 HTTP route handlers."])

    def test_the_route_metadata_drives_existing_diagrams_and_panels(self) -> None:
        result = self._result(self.SOURCE)
        handler = next(
            symbol for symbol in result.symbols if symbol.qualified_name.endswith("Handler.do_GET")
        )

        self.assertEqual(
            [(item["method"], item["path"]) for item in handler.metadata["routes"]],
            [("GET", "/"), ("GET", "/api/evidence/{remainder}")],
        )

    def test_a_method_with_the_conventional_name_in_an_ordinary_class_is_not_a_route(self) -> None:
        result = self._result(
            "class FileWalker:\n"
            "    def do_GET(self):\n"
            "        if self.path == '/tmp':\n"
            "            return None\n"
        )

        self.assertFalse(any(claim.category == "http_route" for claim in result.claims))

    def test_unrelated_literals_inside_a_handler_are_not_routes(self) -> None:
        result = self._result(
            "from http.server import BaseHTTPRequestHandler\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        if self.resource == '/internal':\n"
            "            return None\n"
        )

        self.assertFalse(any(claim.category == "http_route" for claim in result.claims))

    def test_a_standard_handler_fixture_does_not_become_the_served_surface(self) -> None:
        result = self._result(self.SOURCE, "test_server.py")

        self.assertFalse(any(claim.category == "http_route" for claim in result.claims))
        self.assertEqual(
            sum(claim.category == "test_route" for claim in result.claims),
            2,
        )


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


class CollectionDrivenWorksetTests(TestCase):
    """A private imported collection can quietly become another module's scheduler."""

    def _claims(self, source: str, path: str = "worker.py") -> dict[str, str]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
        return {claim.category: claim.claim for claim in result.claims}

    def test_private_imported_collection_defining_a_loop_is_reported(self) -> None:
        source = (
            "from app.store import store\n\n"
            "def tick():\n"
            "    pending = list(store._cache.keys())\n"
            "    for key in pending:\n"
            "        process(key)\n"
        )

        found = self._claims(source)

        self.assertIn("collection_driven_workset", found)
        self.assertIn("store._cache", found["collection_driven_workset"])
        self.assertIn("values not resident", found["collection_driven_workset"])

    def test_local_private_collection_is_not_cross_module_coupling(self) -> None:
        source = (
            "class Worker:\n"
            "    def tick(self):\n"
            "        for key in self._cache.keys():\n"
            "            process(key)\n"
        )

        self.assertNotIn("collection_driven_workset", self._claims(source))

    def test_public_imported_iterable_is_an_ordinary_contract(self) -> None:
        source = (
            "from app.store import store\n\n"
            "def tick():\n"
            "    for key in store.pending_keys:\n"
            "        process(key)\n"
        )

        self.assertNotIn("collection_driven_workset", self._claims(source))

    def test_a_test_loop_does_not_describe_the_product_scheduler(self) -> None:
        source = (
            "from app.store import store\n\n"
            "def test_pending():\n"
            "    for key in store._cache:\n"
            "        assert key\n"
        )

        found = self._claims(source, "tests/test_worker.py")

        self.assertNotIn("collection_driven_workset", found)
        self.assertIn("test_collection_driven_workset", found)


class GlobalCounterTests(TestCase):
    """A `global` name that is not a mutable container.

    Found by pointing the analyzer at an installed third-party library. It
    raised `KeyError` and aborted the entire repository, because two paths
    reach the same map and disagree about which names exist: it is keyed on
    module-owned mutable containers, while the scope check also answers yes to
    anything an enclosing function declared `global`. An integer is not a
    container, so a module-level counter satisfied one and not the other.
    """

    def _mutations(self, source: str) -> dict[str, int]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.py").write_text(source, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
        return {claim.category: 1 for claim in result.claims}

    def test_a_global_counter_does_not_abort_the_analysis(self) -> None:
        # Exactly the shape found in the wild: `n = 0` at module level, then
        # `global n` and `n += 1` inside a function.
        source = "leases = 0\n\n\ndef acquire():\n    global leases\n    leases += 1\n"
        self.assertIsInstance(self._mutations(source), dict)

    def test_a_global_counter_is_recorded_as_process_local_state(self) -> None:
        # A counter rebound across calls is process-local state by the plainest
        # reading of the term, so it is recorded rather than merely survived.
        source = "leases = 0\n\n\ndef acquire():\n    global leases\n    leases += 1\n"
        self.assertIn("process_local_state", self._mutations(source))

    def test_a_global_declared_name_never_assigned_at_module_scope(self) -> None:
        source = "def start():\n    global handle\n    handle = object()\n"
        self.assertIsInstance(self._mutations(source), dict)


class LibrarySurfaceTests(TestCase):
    """Facts a library states about itself, which an application-shaped
    taxonomy had no category for.

    Pointing this at installed third-party packages produced two claims for
    `attrs` and three for `cryptography`. That is not a simple codebase; it is
    a taxonomy built from web applications, where the interesting facts are
    routes and persistence. A library's equivalents are the surface it commits
    to and the parts it has scheduled for removal, and both are ordinary
    Python that any repository may contain.
    """

    def _categories(self, source: str, path: str = "sample.py") -> dict[str, str]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
        return {claim.category: claim.claim for claim in result.claims}

    def test_an_all_declaration_is_recorded_as_a_public_surface(self) -> None:
        found = self._categories('__all__ = ["load", "dump"]\n')
        self.assertIn("public_api", found)
        self.assertIn("2 name(s)", found["public_api"])

    def test_the_exported_names_are_named(self) -> None:
        # A count alone cannot be checked against the source by a reader.
        found = self._categories('__all__ = ["dump", "load"]\n')
        self.assertIn("dump, load", found["public_api"])

    def test_a_computed_all_is_not_reported_as_a_surface(self) -> None:
        # `__all__ = [*base.__all__, "extra"]` has no literal membership.
        self.assertNotIn("public_api", self._categories("__all__ = [*base.__all__]\n"))

    def test_an_all_declaration_in_a_test_is_not_the_product_surface(self) -> None:
        found = self._categories('__all__ = ["fixture"]\n', "tests/test_fixture.py")
        self.assertNotIn("public_api", found)

    def test_a_deprecation_warning_is_recorded(self) -> None:
        source = "import warnings\n\n\ndef old():\n    warnings.warn('x', DeprecationWarning)\n"
        found = self._categories(source)
        self.assertIn("deprecation", found)
        self.assertIn("DeprecationWarning", found["deprecation"])

    def test_an_ordinary_warning_is_not_a_deprecation(self) -> None:
        # A UserWarning reports a condition, not a scheduled removal.
        source = "import warnings\n\n\ndef f():\n    warnings.warn('careful', UserWarning)\n"
        self.assertNotIn("deprecation", self._categories(source))


class DeprecationReceiverTests(TestCase):
    """A deprecation comes from `warnings`, not from anything named `warn`."""

    def _has(self, source: str) -> bool:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.py").write_text(source, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
        return any(claim.category == "deprecation" for claim in result.claims)

    def test_a_logger_call_is_not_a_deprecation(self) -> None:
        # `logger.warn` reports a condition. Matching any `.warn` swept it in.
        source = "import logging\nlog = logging.getLogger()\n\n\ndef f():\n    log.warn('x', DeprecationWarning)\n"
        self.assertFalse(self._has(source))

    def test_the_warnings_module_is_a_deprecation(self) -> None:
        source = "import warnings\n\n\ndef f():\n    warnings.warn('x', DeprecationWarning)\n"
        self.assertTrue(self._has(source))

    def test_a_bare_imported_warn_is_a_deprecation(self) -> None:
        source = "from warnings import warn\n\n\ndef f():\n    warn('x', DeprecationWarning)\n"
        self.assertTrue(self._has(source))


class ExceptionContractTests(TestCase):
    """A package's own error types, and what its handlers absorb.

    The only `try` handling this analyzer had was written to recognise one
    fixture's AI-client fallbacks -- a rule about a repository rather than
    about Python. An outside generator read the same code and produced an
    exception taxonomy: three declared types, all subclassing `ValueError`,
    and the family every handler names. Both are ordinary AST facts and
    neither was recorded here.
    """

    def _parse(self, source: str) -> ast.Module:
        return ast.parse(source)

    def test_a_declared_exception_type_is_recorded(self) -> None:
        found = _defined_exceptions(self._parse("class BadInput(ValueError):\n    pass\n"))
        self.assertEqual(found, [("BadInput", "ValueError", 1)])

    def test_a_plain_class_is_not_an_exception_type(self) -> None:
        self.assertEqual(_defined_exceptions(self._parse("class Config:\n    pass\n")), [])

    def test_a_subclass_of_exception_counts(self) -> None:
        found = _defined_exceptions(self._parse("class Halt(Exception):\n    pass\n"))
        self.assertEqual(found[0][:2], ("Halt", "Exception"))

    def test_a_handler_records_its_whole_family(self) -> None:
        source = "try:\n    go()\nexcept (OSError, ValueError):\n    pass\n"
        self.assertEqual(_caught_families(self._parse(source)), [("OSError, ValueError", 3)])

    def test_a_dotted_exception_keeps_its_module(self) -> None:
        source = "import json\ntry:\n    go()\nexcept json.JSONDecodeError:\n    pass\n"
        self.assertEqual(_caught_families(self._parse(source))[0][0], "json.JSONDecodeError")

    def test_a_bare_except_is_recorded_as_catching_everything(self) -> None:
        # Absorbing everything is a different fact from absorbing something,
        # and a reader should be able to find it.
        source = "try:\n    go()\nexcept:\n    pass\n"
        self.assertEqual(_caught_families(self._parse(source)), [("*", 3)])


class ModuleNameTests(TestCase):
    """A module is named by what could import it, not by where it sits.

    Joining every path segment produced `src.open_skeleton.ledger`, which no
    interpreter can import. It read plausibly enough to survive five repository
    shapes; pointing the tool at a workspace of nine projects rendered it as
    `open-skeleton.src.open_skeleton.ledger` and made it obvious.
    """

    PACKAGE = frozenset({"src/open_skeleton", "src/open_skeleton/analyzers"})

    def test_a_package_root_is_not_part_of_the_name(self) -> None:
        self.assertEqual(
            _module_name("src/open_skeleton/ledger.py", self.PACKAGE), "open_skeleton.ledger"
        )

    def test_a_nested_package_keeps_every_package_segment(self) -> None:
        self.assertEqual(
            _module_name("src/open_skeleton/analyzers/python_ast.py", self.PACKAGE),
            "open_skeleton.analyzers.python_ast",
        )

    def test_an_init_module_is_named_for_its_package(self) -> None:
        self.assertEqual(
            _module_name("src/open_skeleton/__init__.py", self.PACKAGE), "open_skeleton"
        )

    def test_a_layout_with_no_init_anywhere_is_left_alone(self) -> None:
        # A directory without `__init__.py` can still be a package: PEP 420
        # namespace packages are importable, and `from app.core.used import
        # value` proves this one is. Trimming on the absence of a file would
        # rename it to `used` and orphan the import that names it.
        self.assertEqual(_module_name("app/core/used.py", frozenset()), "app.core.used")

    def test_a_file_at_the_root_names_itself(self) -> None:
        self.assertEqual(_module_name("server.py", frozenset()), "server")

    def test_packages_are_recognised_by_their_init_file(self) -> None:
        found = _package_directories(("src/pkg/__init__.py", "src/pkg/a.py", "__init__.py"))
        self.assertEqual(found, frozenset({"src/pkg", ""}))

    def test_two_files_that_share_a_name_are_told_apart_by_import_root(self) -> None:
        # Two distributions in one workspace can each ship a `pkg.mod`, and
        # both names are correct where they live. Sharing a qualified name
        # would merge two unrelated files into a single identity.
        packages = frozenset({"alpha/src/pkg", "beta-app/src/pkg"})
        found = _module_names(("alpha/src/pkg/mod.py", "beta-app/src/pkg/mod.py"), packages)
        self.assertEqual(
            found,
            {
                "alpha/src/pkg/mod.py": "alpha.src.pkg.mod",
                "beta-app/src/pkg/mod.py": "beta_app.src.pkg.mod",
            },
        )

    def test_a_name_that_does_not_collide_carries_no_prefix(self) -> None:
        packages = frozenset({"alpha/src/pkg", "beta/src/other"})
        found = _module_names(("alpha/src/pkg/mod.py", "beta/src/other/mod.py"), packages)
        self.assertEqual(
            found,
            {"alpha/src/pkg/mod.py": "pkg.mod", "beta/src/other/mod.py": "other.mod"},
        )

    def test_every_path_resolves_to_a_distinct_name(self) -> None:
        paths = ("a/src/pkg/mod.py", "b/src/pkg/mod.py", "c/src/pkg/mod.py", "d/other.py")
        packages = frozenset({"a/src/pkg", "b/src/pkg", "c/src/pkg"})
        found = _module_names(paths, packages)
        self.assertEqual(len(set(found.values())), len(paths))


class DeeplyNestedSourceTests(TestCase):
    """One unreadable file is a coverage failure, not an aborted run.

    `sympy/polys/numberfields/resolvent_lookup.py` is a single arithmetic
    expression nested about four hundred nodes deep. `ast.NodeVisitor` recurses
    once per node, so walking it raised `RecursionError` -- from the walk,
    which sat outside the handler that covered parsing -- and a 2,600-file
    repository produced no analysis at all.

    A file this analyzer cannot finish is a file it did not read, and it
    already knows how to say that.
    """

    def _repository(self, root: Path) -> None:
        # A left-associative chain, not nested parentheses. Parentheses hit
        # the parser's own limit and raise `SyntaxError`, which was always
        # handled -- a fixture built from them passes with the guard removed
        # and proves nothing. This parses cleanly into a tree about two
        # thousand nodes deep and fails in the walk, which is the real case.
        (root / "deep.py").write_text("value = " + "+".join(["1"] * 2000) + "\n", encoding="utf-8")
        (root / "shallow.py").write_text("def answer():\n    return 42\n", encoding="utf-8")

    def test_the_run_survives_and_names_the_file_it_could_not_read(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._repository(root)
            result = analyze_snapshot(scan_repository(root))

        coverage = next(item for item in result.coverage if "python" in item.analyzer)
        self.assertEqual(coverage.failed_files, 1)
        self.assertTrue(any("deep.py" in item for item in coverage.failures))

    def test_the_readable_file_is_still_analyzed(self) -> None:
        # The point of the guard is that one file's depth costs one file.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._repository(root)
            result = analyze_snapshot(scan_repository(root))

        self.assertTrue(any(item.path == "shallow.py" for item in result.symbols))

    def test_no_partial_record_from_the_failed_file_reaches_the_result(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._repository(root)
            result = analyze_snapshot(scan_repository(root))

        self.assertEqual([item for item in result.symbols if item.path == "deep.py"], [])


class TestScopedErrorContractTests(TestCase):
    """What a suite absorbs is not the program's error contract.

    The per-claim choke point re-files a test file's claims by category, and
    could not reach the error contract: that claim is aggregated across files
    and emitted once at the end, so a handler inside a suite entered the
    program's contract without passing the check written to stop exactly
    that. This repository reported "1 handler(s) catch `OSError, ValueError`"
    from a test's own `except` around a file it was deliberately failing to
    write.

    A choke point only covers the claims that pass through it, which is the
    part worth remembering.
    """

    def _analyze(self, sources: dict[str, str]) -> AnalysisResult:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, body in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            return analyze_snapshot(scan_repository(root))

    HANDLER = "def go():\n    try:\n        work()\n    except OSError:\n        return None\n"

    def test_a_handler_in_application_code_is_the_contract(self) -> None:
        result = self._analyze({"app.py": self.HANDLER})
        self.assertTrue(
            any(item.category == "caught_exception" for item in result.claims),
            "application code should still declare an error contract",
        )

    def test_a_handler_inside_a_suite_is_not(self) -> None:
        result = self._analyze({"tests/test_app.py": self.HANDLER})
        self.assertFalse(any(item.category == "caught_exception" for item in result.claims))

    def test_a_suite_does_not_dilute_a_real_contract(self) -> None:
        # Both files catch, and only the application one is the program's.
        result = self._analyze(
            {
                "app.py": self.HANDLER,
                "tests/test_app.py": (
                    "def test_go():\n    try:\n        go()\n    except ValueError:\n        pass\n"
                ),
            }
        )
        families = [item.claim for item in result.claims if item.category == "caught_exception"]
        self.assertTrue(families)
        self.assertFalse(any("ValueError" in text for text in families))


class ClientRouteReconciliationTests(TestCase):
    """Both sides of a call, when both sides are in the snapshot.

    A dashboard's document carried "GET /api/adapter is registered as a
    route in server" in one section and "/api/adapter is requested by
    web.app; the server side of this call is whichever route matches it"
    two sections later. The hedge is right in general and needlessly weak
    when the answer is in the same document, and nothing joined them.
    """

    def _claims(self, sources: dict[str, str]) -> list[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, body in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
        return [
            item.claim for item in result.claims if item.category == "client_route_reconciliation"
        ]

    SERVER = (
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
        '@app.get("/api/notes")\ndef notes():\n    return []\n'
    )
    CLIENT = 'export async function load() {\n  return fetch("/api/notes");\n}\n'

    def test_a_served_path_a_client_requests_is_joined(self) -> None:
        found = self._claims({"api.py": self.SERVER, "web/app.ts": self.CLIENT})
        self.assertEqual(len(found), 1)
        self.assertIn("/api/notes", found[0])
        self.assertIn("both sides of that call are in this snapshot", found[0])

    def test_the_join_does_not_claim_the_client_reaches_it(self) -> None:
        # A monorepo can hold two services spelling a path the same way, and
        # nothing here resolves configuration.
        found = self._claims({"api.py": self.SERVER, "web/app.ts": self.CLIENT})
        self.assertIn("not decided here", found[0])

    def test_a_request_with_no_matching_route_is_not_joined(self) -> None:
        client = 'export async function load() {\n  return fetch("/api/absent");\n}\n'
        self.assertEqual(self._claims({"api.py": self.SERVER, "web/app.ts": client}), [])

    def test_a_route_nobody_requests_is_not_joined(self) -> None:
        self.assertEqual(self._claims({"api.py": self.SERVER}), [])


class DottedNameIndexTests(TestCase):
    """A field name without its owner is half a fact.

    `loot_table` does not say what carries it, and `mob.loot_table` is what
    somebody searching an unfamiliar domain model actually types.
    """

    def _index(self, source: str) -> dict[str, int]:
        return _name_index(ast.parse(source))

    def test_an_attribute_records_its_receiver(self) -> None:
        index = self._index("value = mob.loot_table\n")
        self.assertIn("mob.loot_table", index)

    def test_the_bare_attribute_is_still_recorded(self) -> None:
        # Someone who knows only the field name must still find the file.
        index = self._index("value = mob.loot_table\n")
        self.assertIn("loot_table", index)

    def test_a_chain_is_spelled_out_in_full(self) -> None:
        index = self._index("value = player.stats.level\n")
        self.assertIn("player.stats.level", index)

    def test_a_call_receiver_has_no_written_form_and_is_not_invented(self) -> None:
        index = self._index("value = get_player().hp\n")
        self.assertIn("hp", index)
        self.assertFalse([name for name in index if name.endswith(".hp") and "(" in name])
        self.assertNotIn("get_player.hp", index)

    def test_a_subscript_receiver_is_left_alone_too(self) -> None:
        index = self._index("value = items[0].name\n")
        self.assertIn("name", index)
        self.assertNotIn("items.name", index)

    def test_the_line_recorded_is_the_first_occurrence(self) -> None:
        index = self._index("a = 1\nvalue = mob.loot_table\nagain = mob.loot_table\n")
        self.assertEqual(index["mob.loot_table"], 2)


class DeclaredCommandLineTests(TestCase):
    """A `__main__` guard says a module can start, not what a user types.

    This engine's own specification of itself named none of its 106 flags,
    so the document could not answer "how do I run this" about the tool that
    produced it.
    """

    def _flags(self, source: str) -> tuple[list[str], list[str]]:
        options, positionals = _declared_cli_flags(ast.parse(source))
        return sorted(options), sorted(positionals)

    def _lines(self, source: str) -> dict[str, int]:
        options, positionals = _declared_cli_flags(ast.parse(source))
        return {**options, **positionals}

    SOURCE = """\
import argparse

def build():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("repository")
    return parser
"""

    def test_options_are_reported(self) -> None:
        options, _ = self._flags(self.SOURCE)
        self.assertEqual(options, ["--output-dir", "--verbose", "-v"])

    def test_positionals_are_reported_separately(self) -> None:
        _, positionals = self._flags(self.SOURCE)
        self.assertEqual(positionals, ["repository"])

    def test_a_flag_assembled_at_run_time_is_not_guessed(self) -> None:
        # Somebody will type what the document says, so a command line that
        # has no fixed spelling is omitted rather than approximated.
        options, positionals = self._flags(
            "parser.add_argument(f'--{name}')\nparser.add_argument(prefix + 'x')\n"
        )
        self.assertEqual(options, [])
        self.assertEqual(positionals, [])

    def test_a_module_with_no_parser_declares_nothing(self) -> None:
        self.assertEqual(self._flags("x = 1\n"), ([], []))

    def test_the_same_flag_declared_twice_is_reported_once(self) -> None:
        options, _ = self._flags("a.add_argument('--root')\nb.add_argument('--root')\n")
        self.assertEqual(options, ["--root"])

    def test_the_claim_names_the_flags_and_reaches_the_pipeline(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tool.py").write_text(self.SOURCE, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            claim = next(
                item for item in result.claims if item.category == "command_line_interface"
            )
            self.assertIn("`--output-dir`", claim.claim)
            self.assertIn("`repository`", claim.claim)
            self.assertEqual(claim.status, "verified")
            self.assertTrue(claim.supporting_evidence)

    def test_a_flag_is_recorded_with_the_line_that_declares_it(self) -> None:
        lines = self._lines("import argparse\nparser.add_argument('--root')\n")
        self.assertEqual(lines["--root"], 2)

    def test_a_flag_joins_the_searchable_concordance(self) -> None:
        # `--output-dir` is not a Python identifier, so the generic name walk
        # skips it and a reader searching for a flag found nothing.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tool.py").write_text(self.SOURCE, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            index: dict[str, int] = {}
            for symbol in result.symbols:
                index.update(symbol.metadata.get("name_index", {}))
            self.assertIn("--output-dir", index)
            self.assertIn("repository", index)

    def test_a_test_helper_is_not_the_product_command_line(self) -> None:
        # The same rule the rest of the engine uses: a suite's own argument
        # parsing is not the interface the product ships.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            (root / "tests" / "test_tool.py").write_text(self.SOURCE, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            self.assertFalse(
                [item for item in result.claims if item.category == "command_line_interface"]
            )


class RefusalMessageTests(TestCase):
    """A status code says a request was refused; the message says why.

    "Player not found" is the string an operator greps for when the log line
    arrives, and it was read and discarded while the status was kept.
    """

    def _message(self, source: str) -> str | None:
        tree = ast.parse(source)
        raises = [node for node in ast.walk(tree) if isinstance(node, ast.Raise)]
        self.assertEqual(len(raises), 1)
        result: str | None = _raise_message(raises[0])
        return result

    def test_an_http_detail_keyword_is_read(self) -> None:
        self.assertEqual(
            self._message('raise HTTPException(status_code=404, detail="Player not found")'),
            "Player not found",
        )

    def test_a_positional_message_is_read(self) -> None:
        self.assertEqual(
            self._message('raise ValueError("level must be 1-100")'), "level must be 1-100"
        )

    def test_an_interpolated_message_has_no_fixed_text_and_is_not_quoted(self) -> None:
        # A reader will search for the words this document gives them, so a
        # message that cannot be quoted exactly is omitted.
        self.assertIsNone(self._message('raise ValueError(f"bad {value}")'))

    def test_a_message_built_from_a_variable_is_not_quoted(self) -> None:
        self.assertIsNone(self._message("raise ValueError(problem)"))

    def test_a_bare_reraise_carries_no_message(self) -> None:
        self.assertIsNone(self._message("try:\n    pass\nexcept ValueError:\n    raise"))

    def test_the_message_travels_with_the_status_in_control_flow(self) -> None:
        tree = ast.parse(
            'def handler():\n    raise HTTPException(status_code=404, detail="Player not found")\n'
        )
        function = tree.body[0]
        events = _control_flow(function)
        raised = [event for event in events if event["kind"] == "raise"]
        self.assertEqual(raised[0]["label"], "HTTP 404")
        self.assertEqual(raised[0]["message"], "Player not found")
