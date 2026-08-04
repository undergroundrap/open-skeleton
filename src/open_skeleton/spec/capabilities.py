# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Implemented-capability clustering and traceability.

A requirement is a statement of intent. Source code is not intent, so this
module deliberately does not claim to recover requirements. It recovers
**implemented capabilities**: clusters of routes and symbols that the code
actually exposes, each pinned to receipts.

Traceability is then computed rather than asserted. A capability is linked to
the verification that exercises it by following call edges out of test and
operator-harness files, so "this capability has no verifying reference" is a
counted fact a reader can re-derive, not an opinion.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

MIN_CLUSTER_MEMBERS = 1
MAX_CAPABILITIES = 60

_ROUTE_CLAIM = re.compile(r"^(?P<method>[A-Z]+) (?P<path>\S+) is handled by (?P<handler>.+)\.$")


@dataclass(frozen=True, slots=True)
class Capability:
    """One cluster of implemented behavior, with everything that backs it."""

    capability_id: str
    label: str
    kind: str
    routes: tuple[str, ...]
    symbols: tuple[str, ...]
    paths: tuple[str, ...]
    claim_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    exercised_by: tuple[str, ...]

    @property
    def verification(self) -> str:
        return "exercised" if self.exercised_by else "no-verifying-reference"

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "label": self.label,
            "kind": self.kind,
            "routes": list(self.routes),
            "symbols": list(self.symbols),
            "paths": list(self.paths),
            "claim_ids": list(self.claim_ids),
            "evidence_ids": list(self.evidence_ids),
            "exercised_by": list(self.exercised_by),
            "verification": self.verification,
        }


def _short_name(qualified: str) -> str:
    return qualified.rsplit(".", 1)[-1]


def _static_prefix(path: str) -> str:
    """Reduce a route path or URL literal to its leading parameter-free segment.

    A route is declared as ``/action/attack/{player_id}`` but a client builds it
    with an f-string, so the recorded literal is ``/action/attack/``. Comparing
    the static prefix of both sides makes those the same endpoint without
    guessing at the interpolated value.
    """

    return path.split("{", 1)[0]


def verifying_paths(
    files: tuple[dict[str, Any], ...],
    claims: tuple[dict[str, Any], ...],
    evidence_by_id: dict[str, dict[str, Any]],
) -> frozenset[str]:
    """Paths whose calls count as exercising a capability.

    Two sources, both evidence-backed: files the scanner assigned the ``test``
    role, and files cited by ``operator_harness`` claims. The second matters for
    repositories whose real quality gate is a hand-run script rather than a
    conventional suite.
    """

    paths = {str(item["path"]) for item in files if str(item["role"]) == "test"}
    for claim in claims:
        if str(claim["category"]) != "operator_harness":
            continue
        for evidence_id in claim.get("supporting_evidence", ()):
            record = evidence_by_id.get(evidence_id)
            if record is not None and str(record["path"]) not in {".", ""}:
                paths.add(str(record["path"]))
    return frozenset(paths)


def _route_clusters(
    claims: tuple[dict[str, Any], ...],
) -> dict[str, dict[str, set[str]]]:
    clusters: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"routes": set(), "symbols": set(), "claims": set(), "evidence": set()}
    )
    for claim in claims:
        if str(claim["category"]) != "http_route":
            continue
        match = _ROUTE_CLAIM.match(str(claim["claim"]))
        if match is None:
            continue
        path = match.group("path")
        segments = [part for part in path.split("/") if part and not part.startswith("{")]
        label = segments[0] if segments else "root"
        bucket = clusters[label]
        bucket["routes"].add(f"{match.group('method')} {path}")
        bucket["symbols"].add(match.group("handler"))
        bucket["claims"].add(str(claim["claim_id"]))
        bucket["evidence"].update(claim.get("supporting_evidence", ()))
    return clusters


def _module_clusters(
    symbols: tuple[dict[str, Any], ...],
    files: tuple[dict[str, Any], ...],
    claimed_symbols: set[str],
) -> dict[str, dict[str, set[str]]]:
    """Cluster remaining source modules by their containing package directory.

    The containing directory is used rather than the top-level one because a
    ``src/`` layout would otherwise collapse an entire project into a single
    cluster named after its build container.
    """

    source_paths = {str(item["path"]) for item in files if str(item["role"]) == "source"}
    clusters: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"routes": set(), "symbols": set(), "claims": set(), "evidence": set()}
    )
    for symbol in symbols:
        if str(symbol["kind"]) not in {"class", "function", "async_function"}:
            continue
        path = str(symbol["path"])
        if path not in source_paths:
            continue
        qualified = str(symbol["qualified_name"])
        if qualified in claimed_symbols:
            continue
        parts = path.split("/")
        label = parts[-2] if len(parts) > 1 else path.rsplit(".", 1)[0]
        clusters[label]["symbols"].add(qualified)
    return clusters


def build_capabilities(
    *,
    files: tuple[dict[str, Any], ...],
    claims: tuple[dict[str, Any], ...],
    symbols: tuple[dict[str, Any], ...],
    edges: tuple[dict[str, Any], ...],
    evidence_by_id: dict[str, dict[str, Any]],
) -> tuple[Capability, ...]:
    """Cluster implemented capabilities and trace what exercises each one."""

    route_clusters = _route_clusters(claims)
    claimed = {name for bucket in route_clusters.values() for name in bucket["symbols"]}
    module_clusters = _module_clusters(symbols, files, claimed)

    symbol_paths: dict[str, str] = {
        str(item["qualified_name"]): str(item["path"]) for item in symbols
    }
    exercising = verifying_paths(files, claims, evidence_by_id)

    # Index call edges that originate in a verifying file, by callee short name.
    # A verifying file calling its own helpers is self-reference, not coverage,
    # so edges whose target is defined in the calling file are dropped.
    calls_from_verifiers: dict[str, set[str]] = defaultdict(set)
    references_from_verifiers: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        source = str(edge["source_path"])
        if source not in exercising:
            continue
        relationship = str(edge["relationship"])
        target = str(edge["target_ref"])
        if relationship == "calls":
            callee = _short_name(target)
            defined_in = {
                path for name, path in symbol_paths.items() if _short_name(name) == callee
            }
            if defined_in and defined_in <= {source}:
                continue
            calls_from_verifiers[callee].add(source)
        elif relationship == "references_route_path":
            references_from_verifiers[_static_prefix(target)].add(source)

    ordered: list[tuple[str, str, dict[str, set[str]]]] = [
        *((label, "route-group", bucket) for label, bucket in route_clusters.items()),
        *((label, "module", bucket) for label, bucket in module_clusters.items()),
    ]
    ordered.sort(key=lambda item: (item[1] != "route-group", item[0]))

    populated = [item for item in ordered if len(item[2]["symbols"]) >= MIN_CLUSTER_MEMBERS]

    capabilities: list[Capability] = []
    # Identifiers are assigned after filtering so the catalog never shows a gap.
    for index, (label, kind, bucket) in enumerate(populated[:MAX_CAPABILITIES], start=1):
        members = sorted(bucket["symbols"])
        paths = sorted({symbol_paths[name] for name in members if name in symbol_paths})
        route_paths = {entry.split(" ", 1)[1] for entry in bucket["routes"] if " " in entry}
        references = sorted(
            {
                f"{source} calls {_short_name(name)}"
                for name in members
                for source in calls_from_verifiers.get(_short_name(name), ())
            }
            | {
                f"{source} requests {route}"
                for route in route_paths
                for source in references_from_verifiers.get(_static_prefix(route), ())
            }
        )
        capabilities.append(
            Capability(
                capability_id=f"C-{index:03d}",
                label=label,
                kind=kind,
                routes=tuple(sorted(bucket["routes"])),
                symbols=tuple(members),
                paths=tuple(paths),
                claim_ids=tuple(sorted(bucket["claims"])),
                evidence_ids=tuple(sorted(bucket["evidence"])),
                exercised_by=tuple(references),
            )
        )
    return tuple(capabilities)
