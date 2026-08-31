# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Deterministic joins across independently extracted contract surfaces.

The analyzers already recover routes served by source, routes requested by an
in-repository client, and method/path pairs written in Markdown API tables.
Those inventories are more useful when reconciled, but the join has to stay
narrow: equal normalized paths and literal static prefixes are evidence;
similar names, guessed service configuration, and inferred ownership are not.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from open_skeleton.http_targets import local_request_path
from open_skeleton.ids import stable_id

SERVED_ROUTE = re.compile(
    r"^(?:(?P<method>[A-Z]+) )?(?P<path>/\S*) is (?:registered|handled|served)"
)
CLIENT_ROUTE = re.compile(r"^(?P<target>\S+) (?:is requested|begins a request path)")
DOCUMENTED_ROUTE = re.compile(r"^(?P<method>[A-Z]+) (?P<path>/\S+)$")
UNSPECIFIED_METHOD = "method-unspecified"


@dataclass(frozen=True, slots=True)
class ContractRoute:
    """One route spelling reconciled across source, docs, and local callers."""

    contract_id: str
    path: str
    served_methods: tuple[str, ...]
    documented_methods: tuple[str, ...]
    client_relation: str
    documentation_relation: str
    claim_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "path": self.path,
            "served_methods": list(self.served_methods),
            "documented_methods": list(self.documented_methods),
            "client_relation": self.client_relation,
            "documentation_relation": self.documentation_relation,
            "claim_ids": list(self.claim_ids),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(slots=True)
class _Surface:
    methods: set[str]
    claim_ids: set[str]
    evidence_ids: set[str]


def _surface() -> _Surface:
    return _Surface(set(), set(), set())


def _add_claim(surface: _Surface, claim: dict[str, Any], method: str | None = None) -> None:
    if method:
        surface.methods.add(method)
    surface.claim_ids.add(str(claim["claim_id"]))
    surface.evidence_ids.update(str(item) for item in claim.get("supporting_evidence", ()))


def _documentation_relation(served: set[str], documented: set[str]) -> str:
    if not served and not documented:
        return "no served or documented route observed"
    if not served:
        return "documented path not served in this snapshot"
    if not documented:
        return "served path absent from recognized API tables"
    if UNSPECIFIED_METHOD in served:
        return "path agrees; source method is unspecified"
    if served == documented:
        return "method/path agree"
    if served & documented:
        return "path agrees; method sets differ"
    return "path agrees; methods conflict"


def _prefix_compatible(path: str, prefix: str) -> bool:
    """Whether a literal dynamic prefix can denote this exact route path.

    Interpolated path segments leave a trailing slash. A prefix without one
    usually means interpolation happened in the query string, so only exact
    path equality is safe. This keeps `/item` from claiming `/items`.
    """

    return path == prefix or (prefix.endswith("/") and path.startswith(prefix))


def build_contract_concordance(
    *,
    snapshot_id: str,
    claims: tuple[dict[str, Any], ...],
    evidence_by_id: dict[str, dict[str, Any]],
) -> tuple[ContractRoute, ...]:
    """Join exact route contracts without inferring network configuration.

    Documentation endpoints are recovered from the evidence records backing
    the aggregate documented-route claim. This preserves one receipt per table
    row without manufacturing a new source claim during rendering.
    """

    served: defaultdict[str, _Surface] = defaultdict(_surface)
    documented: defaultdict[str, _Surface] = defaultdict(_surface)
    clients: defaultdict[str, _Surface] = defaultdict(_surface)
    prefixes: defaultdict[str, _Surface] = defaultdict(_surface)

    for claim in claims:
        category = str(claim.get("category", ""))
        text = str(claim.get("claim", ""))
        if category == "http_route":
            match = SERVED_ROUTE.match(text)
            if match is not None:
                _add_claim(
                    served[match.group("path")],
                    claim,
                    match.group("method") or UNSPECIFIED_METHOD,
                )
        elif category in {"http_client_route", "http_client_route_prefix"}:
            match = CLIENT_ROUTE.match(text)
            path = local_request_path(match.group("target")) if match is not None else None
            if path is not None:
                target = clients if category == "http_client_route" else prefixes
                _add_claim(target[path], claim)
        elif category == "documented_http_route_inventory":
            for evidence_id in claim.get("supporting_evidence", ()):
                evidence = evidence_by_id.get(str(evidence_id))
                if evidence is None or evidence.get("evidence_kind") != "documented_http_route":
                    continue
                match = DOCUMENTED_ROUTE.match(str(evidence.get("symbol", "")))
                if match is None:
                    continue
                record = documented[match.group("path")]
                _add_claim(record, claim, match.group("method"))
                record.evidence_ids.add(str(evidence_id))

    paths = set(served) | set(documented) | set(clients)
    matched_prefixes: set[str] = set()
    rows: list[ContractRoute] = []
    for path in sorted(paths):
        source = served[path]
        docs = documented[path]
        caller = clients[path]
        compatible = [prefix for prefix in prefixes if _prefix_compatible(path, prefix)]
        matched_prefixes.update(compatible)

        claim_ids = source.claim_ids | docs.claim_ids | caller.claim_ids
        evidence_ids = source.evidence_ids | docs.evidence_ids | caller.evidence_ids
        for prefix in compatible:
            claim_ids.update(prefixes[prefix].claim_ids)
            evidence_ids.update(prefixes[prefix].evidence_ids)

        if caller.claim_ids:
            client_relation = "exact path requested in repository"
        elif compatible:
            client_relation = "compatible static prefix requested in repository"
        else:
            client_relation = "no in-repository caller observed"
        rows.append(
            ContractRoute(
                contract_id=stable_id("contract-route", (snapshot_id, path)),
                path=path,
                served_methods=tuple(sorted(source.methods)),
                documented_methods=tuple(sorted(docs.methods)),
                client_relation=client_relation,
                documentation_relation=_documentation_relation(source.methods, docs.methods),
                claim_ids=tuple(sorted(claim_ids)),
                evidence_ids=tuple(sorted(evidence_ids)),
            )
        )

    # A dynamic request prefix that matches nothing is still contract evidence.
    # Keep it as its own row so absence is visible rather than discarded.
    for prefix in sorted(set(prefixes) - matched_prefixes):
        caller = prefixes[prefix]
        rows.append(
            ContractRoute(
                contract_id=stable_id("contract-route-prefix", (snapshot_id, prefix)),
                path=prefix,
                served_methods=(),
                documented_methods=(),
                client_relation="dynamic request prefix has no compatible route in this snapshot",
                documentation_relation="no served or documented route observed",
                claim_ids=tuple(sorted(caller.claim_ids)),
                evidence_ids=tuple(sorted(caller.evidence_ids)),
            )
        )

    return tuple(sorted(rows, key=lambda item: (item.path, item.contract_id)))
