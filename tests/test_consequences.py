# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from typing import Any
from unittest import TestCase

from open_skeleton.spec.consequences import (
    STANDARD_RULES,
    ClaimCondition,
    ConsequenceRule,
    derive,
)


def _claim(claim_id: str, category: str) -> dict[str, Any]:
    return {"claim_id": claim_id, "category": category, "claim": ""}


class DerivationTests(TestCase):
    def test_a_rule_fires_only_when_every_required_category_is_present(self) -> None:
        rule = ConsequenceRule(rule_id="r", statement="s", when_present=("a", "b"))
        self.assertEqual(derive((_claim("1", "a"),), rules=(rule,)), ())
        fired = derive((_claim("1", "a"), _claim("2", "b")), rules=(rule,))
        self.assertEqual(len(fired), 1)

    def test_a_fired_rule_names_every_claim_it_derived_from(self) -> None:
        rule = ConsequenceRule(rule_id="r", statement="s", when_present=("a",))
        fired = derive((_claim("1", "a"), _claim("2", "a")), rules=(rule,))
        self.assertEqual(fired[0].claim_ids, ("1", "2"))

    def test_a_present_forbidden_category_blocks_the_rule(self) -> None:
        rule = ConsequenceRule(rule_id="r", statement="s", when_present=("a",), when_absent=("b",))
        self.assertEqual(derive((_claim("1", "a"), _claim("2", "b")), rules=(rule,)), ())

    def test_a_probe_confirmed_absence_still_allows_the_rule(self) -> None:
        # A category can appear in the ledger as a counted absence. The profile
        # probe knows it is genuinely missing, and that must win.
        rule = ConsequenceRule(rule_id="r", statement="s", when_present=("a",), when_absent=("b",))
        fired = derive(
            (_claim("1", "a"), _claim("2", "b")),
            absent_categories=frozenset({"b"}),
            rules=(rule,),
        )
        self.assertEqual(len(fired), 1)

    def test_a_rule_with_no_citable_claim_does_not_fire(self) -> None:
        rule = ConsequenceRule(
            rule_id="r", statement="s", when_present=(), when_absent=("b",), cite=("missing",)
        )
        self.assertEqual(derive((_claim("1", "a"),), rules=(rule,)), ())

    def test_results_are_ordered_by_severity(self) -> None:
        rules = (
            ConsequenceRule(rule_id="low", statement="s", when_present=("a",), severity="medium"),
            ConsequenceRule(rule_id="top", statement="s", when_present=("a",), severity="critical"),
        )
        fired = derive((_claim("1", "a"),), rules=rules)
        self.assertEqual([item.rule_id for item in fired], ["top", "low"])

    def test_a_text_condition_filters_one_category_without_citing_nonmatches(self) -> None:
        rule = ConsequenceRule(
            rule_id="r",
            statement="s",
            when_present=("boundary",),
            when_matching=(ClaimCondition("route", r"^POST\s"),),
        )
        claims = (
            _claim("boundary", "boundary"),
            {**_claim("get", "route"), "claim": "GET /health is handled by app.health."},
            {**_claim("post", "route"), "claim": "POST /save is handled by app.save."},
        )

        fired = derive(claims, rules=(rule,))

        self.assertEqual(fired[0].claim_ids, ("boundary", "post"))

    def test_a_text_condition_does_not_fire_on_an_adjacent_nonmatch(self) -> None:
        rule = ConsequenceRule(
            rule_id="r",
            statement="s",
            when_matching=(ClaimCondition("route", r"^POST\s"),),
        )
        get_only = ({**_claim("get", "route"), "claim": "GET /health is handled."},)

        self.assertEqual(derive(get_only, rules=(rule,)), ())

    def test_output_is_deterministic(self) -> None:
        claims = (_claim("2", "a"), _claim("1", "a"))
        first = derive(claims, rules=STANDARD_RULES[:2])
        second = derive(claims, rules=STANDARD_RULES[:2])
        self.assertEqual([item.to_dict() for item in first], [item.to_dict() for item in second])


class StandardRuleTests(TestCase):
    def test_every_shipped_rule_cites_something(self) -> None:
        for rule in STANDARD_RULES:
            self.assertTrue(rule.basis(), f"{rule.rule_id} cites no category")

    def test_every_shipped_rule_has_a_unique_identifier(self) -> None:
        identifiers = [rule.rule_id for rule in STANDARD_RULES]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_no_shipped_rule_recommends_an_action(self) -> None:
        # A consequence states what follows. Telling the reader what to do is a
        # judgement this engine has no evidence for.
        banned = ("should ", "must ", "recommend", "you need", "consider ")
        for rule in STANDARD_RULES:
            lowered = rule.statement.lower()
            for phrase in banned:
                self.assertNotIn(phrase, lowered, f"{rule.rule_id} prescribes: {phrase!r}")

    def test_unauthenticated_open_origin_needs_both_findings(self) -> None:
        rules = tuple(
            item for item in STANDARD_RULES if item.rule_id == "unauthenticated-open-origin"
        )
        self.assertEqual(derive((_claim("1", "auth_control_census"),), rules=rules), ())
        self.assertEqual(
            derive(
                (_claim("1", "auth_control_census"), _claim("2", "security_boundary")),
                rules=rules,
            ),
            (),
        )
        fired = derive(
            (
                _claim("1", "auth_control_census"),
                _claim("2", "security_boundary"),
                {**_claim("3", "http_route"), "claim": "GET /health is handled."},
            ),
            rules=rules,
        )
        self.assertEqual(fired[0].severity, "high")

    def test_cross_origin_mutation_requires_a_state_changing_route(self) -> None:
        rules = tuple(
            item for item in STANDARD_RULES if item.rule_id == "cross-origin-mutation-reachability"
        )
        shared = (_claim("1", "auth_control_census"), _claim("2", "security_boundary"))
        get_route = {**_claim("get", "http_route"), "claim": "GET /state is handled."}
        post_route = {**_claim("post", "http_route"), "claim": "POST /state is handled."}

        self.assertEqual(derive((*shared, get_route), rules=rules), ())
        fired = derive((*shared, get_route, post_route), rules=rules)

        self.assertEqual(fired[0].severity, "critical")
        self.assertEqual(fired[0].claim_ids, ("1", "2", "post"))
