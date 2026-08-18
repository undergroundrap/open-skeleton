# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Structures that carry more than one concern at once.

The most useful sentences in a long specification are rarely single facts.
They are coincidences: one structure turning out to serve two purposes, and
the consequences that follow from the two being the same object. A cache that
is also the work queue means a zone never loaded is never simulated. A route
handler that also reconciles state means a restart is visible to a client.
Neither consequence is present in either fact alone.

That reads like knowledge of the codebase and is not. It is the result of
asking, for every structure, what *else* it does — a question the ledger can
already answer, because a claim names the symbol it is about and a structure
appearing under two unrelated categories is exactly a structure with two jobs.

The filtering is what makes it worth reading. Every HTTP route also produces a
route-inventory claim and a framework-behaviour claim; those are facets of one
concern rather than a second job, and reporting them would bury the real
coincidences in taxonomy. Categories are therefore grouped into concern
families, and only a structure spanning two families is reported.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

# Concern families. Two claims from one family describe the same job from
# different angles; two from different families are a structure doing two jobs.
CATEGORY_FAMILY: dict[str, str] = {
    "http_route": "interface",
    "http_route_inventory": "interface",
    "http_framework_behavior": "interface",
    "http_client_inventory": "interface",
    "test_route": "interface",
    "process_local_state": "state",
    "storage_schema": "state",
    "state_reconciliation": "state",
    "schema_migration": "state",
    "browser_storage": "state",
    "ui_state": "state",
    "security_boundary": "security",
    "auth_control": "security",
    "data_protection": "security",
    "third_party_origin": "security",
    "unsafe_surface": "security",
    "testing": "verification",
    "operator_harness": "verification",
    "panic_site": "verification",
    "failure_surface": "verification",
    "error_surface": "verification",
    "hardcoded_endpoint": "integration",
    "configuration_read": "integration",
    "external_calls": "integration",
    "absorbed_failure": "integration",
    "exponential_scaling": "domain",
    "mathematical_conflict": "domain",
    "delivery_automation": "delivery",
    "process_termination": "delivery",
    "application_entry": "delivery",
    "compiler_configuration": "delivery",
    "concentration": "maintenance",
    "orphan_candidate": "maintenance",
    "documentation_drift": "maintenance",
    "api_documentation_drift": "maintenance",
    # A documented route is a claim about the README, not about the served
    # interface. Filing it under interface paired every drifting path with
    # its own drift claim and called that a structure with two jobs.
    "documented_http_route_inventory": "maintenance",
    "dependency_drift": "maintenance",
    "trait_implementation": "contract",
    "model_fields": "contract",
}
# A census claim attaches to everything it surveyed by construction, so it says
# nothing about any one structure and would make every symbol look multi-role.
CENSUS_CATEGORIES = frozenset(
    {"auth_control_census", "testing_census", "http_route_inventory", "concentration"}
)


@dataclass(frozen=True, slots=True)
class MultiRole:
    """One structure and the unrelated concerns it turns out to carry."""

    structure: str
    location: str
    families: tuple[str, ...]
    categories: tuple[str, ...]
    claim_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "structure": self.structure,
            "location": self.location,
            "families": list(self.families),
            "categories": list(self.categories),
            "claim_ids": list(self.claim_ids),
        }


def derive_roles(
    claims: tuple[dict[str, Any], ...],
    evidence: tuple[dict[str, Any], ...],
) -> tuple[MultiRole, ...]:
    """Structures that appear under concerns from more than one family.

    Nothing is asserted that a claim did not already say. What is added is the
    observation that two of them are about the same object, which is the fact
    neither claim could carry alone.
    """

    evidence_by_id = {str(item["evidence_id"]): item for item in evidence}
    categories: dict[str, set[str]] = defaultdict(set)
    located: dict[str, str] = {}
    claim_ids: dict[str, set[str]] = defaultdict(set)

    for claim in claims:
        category = str(claim.get("category", ""))
        if category in CENSUS_CATEGORIES or category not in CATEGORY_FAMILY:
            continue
        for evidence_id in claim.get("supporting_evidence", ()) or ():
            record = evidence_by_id.get(str(evidence_id))
            if record is None:
                continue
            symbol = str(record.get("symbol") or "")
            path = str(record.get("path") or "")
            if not symbol or path in {".", ""}:
                continue
            categories[symbol].add(category)
            claim_ids[symbol].add(str(claim.get("claim_id", "")))
            start = record.get("start_line")
            located.setdefault(symbol, f"{path}:{start}" if start else path)

    results: list[MultiRole] = []
    for symbol, found in categories.items():
        families = {CATEGORY_FAMILY[item] for item in found}
        if len(families) < 2:
            continue
        results.append(
            MultiRole(
                structure=symbol,
                location=located.get(symbol, "—"),
                families=tuple(sorted(families)),
                categories=tuple(sorted(found)),
                claim_ids=tuple(sorted(claim_ids[symbol])),
            )
        )
    # Most families first: a structure carrying four concerns is the one where
    # a change is least likely to do only what its author intended.
    return tuple(sorted(results, key=lambda item: (-len(item.families), item.structure)))
