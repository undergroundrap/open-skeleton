# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from typing import Any
from unittest import TestCase

from open_skeleton.spec.diagrams import Diagram, build_diagrams


def _claim(category: str, text: str) -> dict[str, Any]:
    return {"category": category, "claim": text, "claim_id": text[:16]}


class RouteSequenceTests(TestCase):
    SYMBOLS = (
        {
            "symbol_id": "s_attack",
            "qualified_name": "backend.main.attack",
            "kind": "async_function",
            "path": "backend/main.py",
            "start_line": 100,
        },
        {
            "symbol_id": "s_db",
            "qualified_name": "backend.main.vec_db",
            "kind": "module_variable",
            "path": "backend/main.py",
            "start_line": 10,
        },
        # A module-level `player` in an unrelated file must not turn a local
        # named `player` in main.py into a participant.
        {
            "symbol_id": "s_other",
            "qualified_name": "backend.other.player",
            "kind": "module_variable",
            "path": "backend/other.py",
            "start_line": 5,
        },
    )
    CLAIMS = (_claim("http_route", "POST /action/attack/{id} is handled by backend.main.attack."),)
    EVIDENCE = {
        "e_deco": {"start_line": 99},
        "e_db": {"start_line": 105},
        "e_local": {"start_line": 106},
        "e_dup": {"start_line": 110},
    }

    def _edges(self) -> tuple[dict[str, Any], ...]:
        return (
            # Decorator call, above the `def` line.
            {
                "relationship": "calls",
                "source_symbol_id": "s_attack",
                "source_path": "backend/main.py",
                "target_ref": "app.post",
                "evidence_id": "e_deco",
            },
            {
                "relationship": "calls",
                "source_symbol_id": "s_attack",
                "source_path": "backend/main.py",
                "target_ref": "vec_db.get_player",
                "evidence_id": "e_db",
            },
            {
                "relationship": "calls",
                "source_symbol_id": "s_attack",
                "source_path": "backend/main.py",
                "target_ref": "player.get",
                "evidence_id": "e_local",
            },
            {
                "relationship": "calls",
                "source_symbol_id": "s_attack",
                "source_path": "backend/main.py",
                "target_ref": "vec_db.get_player",
                "evidence_id": "e_dup",
            },
        )

    def _build(self) -> tuple[Diagram, ...]:
        return build_diagrams(
            "route_sequence",
            files=(),
            claims=self.CLAIMS,
            symbols=self.SYMBOLS,
            edges=self._edges(),
            evidence_by_id=self.EVIDENCE,
        )

    def test_module_owned_collaborator_becomes_a_participant(self) -> None:
        diagram = self._build()[0]
        assert diagram.mermaid is not None
        self.assertIn("participant vec_db", diagram.mermaid)
        self.assertIn("get_player()", diagram.mermaid)

    def test_local_value_from_another_file_is_not_a_participant(self) -> None:
        diagram = self._build()[0]
        assert diagram.mermaid is not None
        self.assertNotIn("participant player", diagram.mermaid)

    def test_decorator_registration_is_not_a_message(self) -> None:
        diagram = self._build()[0]
        assert diagram.mermaid is not None
        self.assertNotIn("post()", diagram.mermaid)

    def test_repeated_call_is_drawn_once(self) -> None:
        diagram = self._build()[0]
        assert diagram.mermaid is not None
        self.assertEqual(diagram.mermaid.count("attack->>vec_db: get_player()"), 1)

    def test_no_resolvable_collaborator_is_omitted_with_a_reason(self) -> None:
        diagrams = build_diagrams(
            "route_sequence",
            files=(),
            claims=self.CLAIMS,
            symbols=self.SYMBOLS,
            edges=(),
            evidence_by_id={},
        )
        self.assertIsNone(diagrams[0].mermaid)
        assert diagrams[0].omitted_reason is not None
        self.assertIn("without guessing", diagrams[0].omitted_reason)

    def test_limit_bounds_the_number_of_sequences(self) -> None:
        claims = tuple(
            _claim(
                "http_route",
                f"POST /a{index}/ is handled by backend.main.attack.",
            )
            for index in range(5)
        )
        diagrams = build_diagrams(
            "route_sequence",
            files=(),
            claims=claims,
            symbols=self.SYMBOLS,
            edges=self._edges(),
            evidence_by_id=self.EVIDENCE,
            route_sequence_limit=2,
        )
        self.assertEqual(len(diagrams), 2)


class PersistenceErdTests(TestCase):
    CLAIMS = (
        _claim("storage_schema", "app.db.DBManager._init_tables creates SQLite table players."),
        _claim(
            "storage_serialization",
            "app.db.DBManager.save_player serializes JSON into SQLite table players.",
        ),
    )

    def test_tables_and_accessors_are_drawn_from_claims(self) -> None:
        diagram = build_diagrams(
            "persistence_erd",
            files=(),
            claims=self.CLAIMS,
            symbols=(),
            edges=(),
            evidence_by_id={},
        )[0]
        assert diagram.mermaid is not None
        self.assertIn('t_players[("players")]', diagram.mermaid)
        self.assertIn("creates", diagram.mermaid)
        self.assertIn("writes", diagram.mermaid)
        self.assertEqual(diagram.edge_count, 2)

    def test_no_table_claim_omits_rather_than_inventing_a_schema(self) -> None:
        diagram = build_diagrams(
            "persistence_erd",
            files=(),
            claims=(),
            symbols=(),
            edges=(),
            evidence_by_id={},
        )[0]
        self.assertIsNone(diagram.mermaid)
        assert diagram.omitted_reason is not None
        self.assertIn("No claim records a durable table", diagram.omitted_reason)


class UnknownGeneratorTests(TestCase):
    def test_unregistered_name_reports_rather_than_raises(self) -> None:
        diagram = build_diagrams(
            "state_machine",
            files=(),
            claims=(),
            symbols=(),
            edges=(),
            evidence_by_id={},
        )[0]
        self.assertIsNone(diagram.mermaid)
        assert diagram.omitted_reason is not None
        self.assertIn("No generator is registered", diagram.omitted_reason)


class HandlerFlowTests(TestCase):
    SYMBOLS = (
        {
            "symbol_id": "s_attack",
            "qualified_name": "backend.main.attack",
            "kind": "async_function",
            "path": "backend/main.py",
            "start_line": 100,
            "metadata": {
                "control_flow": [
                    {"kind": "guard", "line": 102, "label": "not player", "depth": 0},
                    {"kind": "raise", "line": 103, "label": "HTTP 404", "depth": 1},
                    {"kind": "return", "line": 110, "label": "{'ok': True}", "depth": 0},
                ]
            },
        },
        {
            "symbol_id": "s_plain",
            "qualified_name": "backend.main.ping",
            "kind": "async_function",
            "path": "backend/main.py",
            "start_line": 200,
            "metadata": {
                "control_flow": [
                    {"kind": "return", "line": 201, "label": "{'ok': True}", "depth": 0}
                ]
            },
        },
    )

    def _build(self, *claims: dict[str, Any]) -> tuple[Diagram, ...]:
        return build_diagrams(
            "handler_flow",
            files=(),
            claims=claims,
            symbols=self.SYMBOLS,
            edges=(),
            evidence_by_id={},
        )

    def test_guards_become_decisions_and_raises_become_rejections(self) -> None:
        diagram = self._build(_claim("http_route", "POST /a is handled by backend.main.attack."))[0]
        assert diagram.mermaid is not None
        self.assertIn('{"not player<br/>L102"}', diagram.mermaid)
        self.assertIn("reject: HTTP 404", diagram.mermaid)
        self.assertIn("return {'ok': True}", diagram.mermaid)

    def test_every_node_carries_its_line(self) -> None:
        diagram = self._build(_claim("http_route", "POST /a is handled by backend.main.attack."))[0]
        assert diagram.mermaid is not None
        for line in ("L102", "L103", "L110"):
            self.assertIn(line, diagram.mermaid)

    def test_a_handler_with_no_decision_is_not_drawn(self) -> None:
        diagrams = self._build(_claim("http_route", "GET /ping is handled by backend.main.ping."))
        self.assertIsNone(diagrams[0].mermaid)
        assert diagrams[0].omitted_reason is not None
        self.assertIn("no decision structure", diagrams[0].omitted_reason)

    def test_quotes_in_a_label_do_not_break_mermaid(self) -> None:
        symbols = (
            {
                "symbol_id": "s",
                "qualified_name": "m.h",
                "kind": "function",
                "path": "m.py",
                "start_line": 1,
                "metadata": {
                    "control_flow": [
                        {"kind": "guard", "line": 2, "label": 'x == "a|b"', "depth": 0},
                        {"kind": "return", "line": 3, "label": "1", "depth": 0},
                    ]
                },
            },
        )
        diagram = build_diagrams(
            "handler_flow",
            files=(),
            claims=(_claim("http_route", "GET /x is handled by m.h."),),
            symbols=symbols,
            edges=(),
            evidence_by_id={},
        )[0]
        assert diagram.mermaid is not None
        self.assertNotIn('"a|b"', diagram.mermaid)
        self.assertIn("'a/b'", diagram.mermaid)

    def test_limit_bounds_the_number_of_flows(self) -> None:
        claims = tuple(
            _claim("http_route", f"POST /r{index} is handled by backend.main.attack.")
            for index in range(5)
        )
        diagrams = build_diagrams(
            "handler_flow",
            files=(),
            claims=claims,
            symbols=self.SYMBOLS,
            edges=(),
            evidence_by_id={},
            handler_flow_limit=2,
        )
        self.assertEqual(len(diagrams), 2)
