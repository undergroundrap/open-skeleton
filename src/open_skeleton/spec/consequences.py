# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Engineering consequences derived from combinations of claims.

A specification that stops at facts leaves the reader to do the reasoning. Eight
separate claims saying "this dictionary is process-local" mean one thing
together — a second instance of this process would enforce none of the first
one's state — and that sentence is what a reader actually needs.

Each rule here fires on the presence or absence of claim categories and names
every claim it derived from. Nothing is inferred beyond the composition itself:
if the facts hold, the consequence holds, and a reader can check both. A rule
that would need to guess at intent is not a rule and does not belong here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ClaimCondition:
    """A category whose claim text must match a deterministic pattern."""

    category: str
    pattern: str


@dataclass(frozen=True, slots=True)
class ConsequenceRule:
    """One derivation: categories that must be present, absent, and what follows."""

    rule_id: str
    statement: str
    when_present: tuple[str, ...] = ()
    when_absent: tuple[str, ...] = ()
    when_matching: tuple[ClaimCondition, ...] = ()
    severity: str = "medium"
    # Categories whose claims are cited as the derivation's basis. Defaults to
    # `when_present` when unset.
    cite: tuple[str, ...] = ()

    def basis(self) -> tuple[str, ...]:
        if self.cite:
            return self.cite
        return tuple(
            dict.fromkeys(
                (*self.when_present, *(condition.category for condition in self.when_matching))
            )
        )


@dataclass(frozen=True, slots=True)
class Consequence:
    rule_id: str
    statement: str
    severity: str
    derived_from: tuple[str, ...] = field(default_factory=tuple)
    claim_ids: tuple[str, ...] = field(default_factory=tuple)
    absent_categories: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "statement": self.statement,
            "severity": self.severity,
            "derived_from": list(self.derived_from),
            "claim_ids": list(self.claim_ids),
            "absent_categories": list(self.absent_categories),
        }


# Every rule composes facts this engine already verifies. The statement says what
# follows from the combination, never what the authors intended or should do.
STANDARD_RULES: tuple[ConsequenceRule, ...] = (
    ConsequenceRule(
        rule_id="single-instance-required",
        statement=(
            "State that survives only inside the process is held with no shared "
            "cache or broker declared, so a second concurrent instance would "
            "observe none of the first one's values. Running more than one "
            "process changes behaviour rather than adding capacity."
        ),
        when_present=("process_local_state",),
        when_absent=("cache_dependency",),
        severity="high",
    ),
    ConsequenceRule(
        rule_id="unauthenticated-open-origin",
        statement=(
            "The application declares a wildcard browser-origin policy while no "
            "detected route signature declares an authorization dependency. The "
            "declared CORS policy permits eligible browser responses to be read by "
            "arbitrary origins without a declared route-level identity check."
        ),
        when_present=("auth_control_census", "security_boundary", "http_route"),
        severity="high",
    ),
    ConsequenceRule(
        rule_id="cross-origin-mutation-reachability",
        statement=(
            "The served surface includes POST, PUT, PATCH, or DELETE, declares a "
            "wildcard browser-origin policy, and declares no route-level "
            "authorization dependency. At the route-signature layer, no identity "
            "check stands between an eligible cross-origin request using that method "
            "and handler entry; middleware and infrastructure remain outside this "
            "derivation."
        ),
        when_present=("auth_control_census", "security_boundary"),
        when_matching=(ClaimCondition("http_route", r"^(?:POST|PUT|PATCH|DELETE)\s"),),
        severity="critical",
    ),
    ConsequenceRule(
        rule_id="schema-change-is-manual",
        statement=(
            "A durable store exists with no migration tooling or migration "
            "directory in the snapshot, so a change to the stored shape has no "
            "recorded upgrade path for data already written."
        ),
        when_present=("storage_schema",),
        when_absent=("schema_migration",),
        severity="medium",
    ),
    ConsequenceRule(
        rule_id="concentration-without-tests",
        statement=(
            "The largest files in the repository carry no automated test "
            "reference, so the code most expensive to change is also the code "
            "with the least mechanical protection against changing it wrongly."
        ),
        when_present=("concentration", "testing_gap"),
        severity="high",
    ),
    ConsequenceRule(
        rule_id="undeclared-runtime-dependency",
        statement=(
            "Source imports a package that no dependency manifest declares, so a "
            "clean environment built from the manifests alone will fail at the "
            "first import rather than at install time."
        ),
        when_present=("dependency_drift",),
        severity="high",
    ),
    ConsequenceRule(
        rule_id="documented-surface-drift",
        statement=(
            "The documented interface and the served interface disagree. A "
            "consumer written against the documentation will call routes that do "
            "not exist, or miss routes that do."
        ),
        when_present=("api_documentation_drift",),
        severity="high",
    ),
    ConsequenceRule(
        rule_id="no-release-automation",
        statement=(
            "No pipeline definition exists in the snapshot, so every check this "
            "repository documents runs only when a person remembers to run it. "
            "Nothing mechanically prevents a change that fails them from landing."
        ),
        when_present=("delivery_automation",),
        when_absent=("pipeline_definition",),
        severity="medium",
    ),
    ConsequenceRule(
        rule_id="restart-loses-session-state",
        statement=(
            "Process-local state includes values a client can observe, and a "
            "reconciliation routine exists to repair references after a restart. "
            "Restarting the process is therefore a visible event to a connected "
            "client, not a transparent one."
        ),
        when_present=("process_local_state", "state_reconciliation"),
        severity="medium",
    ),
    ConsequenceRule(
        rule_id="panics-are-the-error-path",
        statement=(
            "Panicking call sites are present with no aggregated error reporting "
            "declared, so a panic in production terminates the thread without "
            "leaving a record anywhere the operator will look."
        ),
        when_present=("panic_site",),
        when_absent=("error_tracking",),
        severity="high",
    ),
    ConsequenceRule(
        rule_id="unsafe-without-audit",
        statement=(
            "Code opts out of the compiler's guarantees while no dependency "
            "vulnerability scanning is configured, so neither the hand-written "
            "escape hatches nor the dependency tree is being checked mechanically."
        ),
        when_present=("unsafe_surface",),
        when_absent=("vulnerability_scanning",),
        severity="high",
    ),
)

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def derive(
    claims: tuple[dict[str, Any], ...],
    *,
    absent_categories: frozenset[str] = frozenset(),
    rules: tuple[ConsequenceRule, ...] = STANDARD_RULES,
) -> tuple[Consequence, ...]:
    """Fire every rule whose conditions the ledger satisfies.

    `absent_categories` names concerns a profile probe determined are missing.
    A rule requiring absence fires only when the category is genuinely absent
    from both the claim ledger and that set, so a missing analyzer cannot be
    mistaken for a missing feature.
    """

    present: dict[str, list[str]] = {}
    claims_by_category: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        category = str(claim["category"])
        present.setdefault(category, []).append(str(claim["claim_id"]))
        claims_by_category.setdefault(category, []).append(claim)

    derived: list[Consequence] = []
    for rule in rules:
        if any(category not in present for category in rule.when_present):
            continue
        matched: dict[str, list[str]] = {}
        conditions_hold = True
        for condition in rule.when_matching:
            identifiers = [
                str(claim["claim_id"])
                for claim in claims_by_category.get(condition.category, ())
                if re.search(condition.pattern, str(claim.get("claim", "")))
            ]
            if not identifiers:
                conditions_hold = False
                break
            matched.setdefault(condition.category, []).extend(identifiers)
        if not conditions_hold:
            continue
        blocked = [
            category
            for category in rule.when_absent
            if category in present and category not in absent_categories
        ]
        if blocked:
            continue
        claim_ids = tuple(
            sorted(
                {
                    claim_id
                    for category in rule.basis()
                    for claim_id in matched.get(category, present.get(category, []))
                }
            )
        )
        if not claim_ids:
            continue
        derived.append(
            Consequence(
                rule_id=rule.rule_id,
                statement=rule.statement,
                severity=rule.severity,
                derived_from=rule.basis(),
                claim_ids=claim_ids,
                absent_categories=rule.when_absent,
            )
        )
    derived.sort(key=lambda item: (_SEVERITY_ORDER.get(item.severity, 3), item.rule_id))
    return tuple(derived)
