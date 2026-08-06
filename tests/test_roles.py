# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

from typing import Any
from unittest import TestCase

from open_skeleton.spec.roles import derive_roles


def _pair(claim_id: str, category: str, evidence_id: str) -> dict[str, Any]:
    return {"claim_id": claim_id, "category": category, "supporting_evidence": [evidence_id]}


def _receipt(evidence_id: str, symbol: str) -> dict[str, Any]:
    return {"evidence_id": evidence_id, "symbol": symbol, "path": "app.py", "start_line": 4}


class MultiRoleTests(TestCase):
    """The useful sentences are coincidences, not single facts."""

    def test_a_structure_spanning_two_families_is_reported(self) -> None:
        # A route handler that also reconciles state means a restart is visible
        # to a client — a consequence neither claim carries alone.
        roles = derive_roles(
            (_pair("c1", "http_route", "e1"), _pair("c2", "state_reconciliation", "e2")),
            (_receipt("e1", "app.login"), _receipt("e2", "app.login")),
        )
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0].structure, "app.login")
        self.assertEqual(roles[0].families, ("interface", "state"))

    def test_two_facets_of_one_concern_are_not_a_second_job(self) -> None:
        # Every route also produces a framework-behaviour claim. Counting that
        # would report taxonomy as coincidence.
        roles = derive_roles(
            (_pair("c1", "http_route", "e1"), _pair("c2", "http_framework_behavior", "e2")),
            (_receipt("e1", "app.login"), _receipt("e2", "app.login")),
        )
        self.assertEqual(roles, ())

    def test_a_census_claim_does_not_make_a_structure_multi_role(self) -> None:
        # A census attaches to everything it surveyed by construction.
        roles = derive_roles(
            (_pair("c1", "http_route", "e1"), _pair("c2", "auth_control_census", "e2")),
            (_receipt("e1", "app.login"), _receipt("e2", "app.login")),
        )
        self.assertEqual(roles, ())

    def test_claims_about_different_structures_are_not_a_coincidence(self) -> None:
        roles = derive_roles(
            (_pair("c1", "http_route", "e1"), _pair("c2", "state_reconciliation", "e2")),
            (_receipt("e1", "app.login"), _receipt("e2", "app.other")),
        )
        self.assertEqual(roles, ())

    def test_a_repository_wide_receipt_names_no_structure(self) -> None:
        roles = derive_roles(
            (_pair("c1", "http_route", "e1"), _pair("c2", "storage_schema", "e2")),
            (
                {"evidence_id": "e1", "symbol": "app.login", "path": "."},
                {"evidence_id": "e2", "symbol": "app.login", "path": "."},
            ),
        )
        self.assertEqual(roles, ())

    def test_the_busiest_structure_is_reported_first(self) -> None:
        claims = (
            _pair("a1", "http_route", "x1"),
            _pair("a2", "storage_schema", "x2"),
            _pair("a3", "operator_harness", "x3"),
            _pair("b1", "http_route", "y1"),
            _pair("b2", "storage_schema", "y2"),
        )
        evidence = tuple(_receipt(f"x{i}", "app.busy") for i in (1, 2, 3)) + tuple(
            _receipt(f"y{i}", "app.quiet") for i in (1, 2)
        )
        roles = derive_roles(claims, evidence)
        self.assertEqual(roles[0].structure, "app.busy")
