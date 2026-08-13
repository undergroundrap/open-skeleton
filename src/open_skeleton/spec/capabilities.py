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

# Directories that hold code without describing it. Clustering by the
# containing folder named a whole browser game "src", which is the build
# layout rather than anything the program does; the modules inside it are
# called movement, combat and renderer, and those are its capabilities.
BUILD_CONTAINERS = frozenset({"src", "lib", "app", "source", "js", "ts", "scripts", "code"})
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
    """The final segment of a qualified name, in either separator.

    Splitting on `.` alone silently passed every Rust name through unchanged,
    so `crates::warmboot_core::compat::check_build` never matched a call edge
    recorded as `check_build`. The comparison was between a full path and a
    bare name, which cannot succeed, and the result was reported as a crate
    with no verification rather than as a name that failed to normalize.
    """

    return qualified.rsplit("::", 1)[-1].rsplit(".", 1)[-1]


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

    Three sources, all evidence-backed: files the scanner assigned the ``test``
    role, files cited by ``operator_harness`` claims, and files cited by
    ``testing`` claims. The second matters for repositories whose real quality
    gate is a hand-run script rather than a conventional suite.

    The third exists because whole-file role is the wrong unit for some
    languages. Rust's convention puts tests in a ``#[cfg(test)] mod tests``
    block inside the file they cover, so the file's role is ``source`` and the
    tests inside it were invisible here. A 52-module crate with 178 passing
    tests reported every capability as having no verifying reference -- a
    statement about this tracer, presented as a statement about the code.

    Granularity is still the whole file, the same limitation the role-based
    source has always had: a file is treated as exercising what it calls, and
    the tests inside it are not separated from the rest.
    """

    paths = {str(item["path"]) for item in files if str(item["role"]) == "test"}
    for claim in claims:
        if str(claim["category"]) not in {"operator_harness", "testing"}:
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
    nested layout would otherwise collapse an entire project into one cluster.
    When that directory is itself a build container the module names the
    cluster instead, since ``src`` describes where code was put rather than
    what it does.
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
        folder = parts[-2] if len(parts) > 1 else ""
        stem = parts[-1].rsplit(".", 1)[0]
        # A build container is not a capability boundary. When the nearest
        # folder is one, the module names itself.
        label = stem if (not folder or folder.casefold() in BUILD_CONTAINERS) else folder
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
    # A dedicated test file calling its own helpers is self-reference rather
    # than coverage, so edges whose target it also defines are dropped.
    #
    # That rule inverts for a source file carrying inline tests. Rust puts a
    # `#[cfg(test)] mod tests` block in the file it covers, so a same-file call
    # is not a helper -- it is the test calling the thing under test, which is
    # the whole convention. Applying the dedicated-file rule to it discarded
    # every inline test in the repository and reported a crate with 178 passing
    # tests as having no verifying reference anywhere.
    #
    # The predicate is not the file's role. A hand-run harness under `scripts/`
    # is a whole verification artifact too, and its self-calls are helpers just
    # as a test file's are. What differs is a source file that merely contains
    # tests, so those are the only ones exempted.
    inline_only = {
        str(record["path"])
        for claim in claims
        if str(claim["category"]) == "testing"
        for evidence_id in claim.get("supporting_evidence", ())
        if (record := evidence_by_id.get(evidence_id)) is not None
        and str(record["path"]) not in {".", ""}
    } - {str(item["path"]) for item in files if str(item["role"]) == "test"}
    dedicated = exercising - inline_only
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
            if source in dedicated and defined_in and defined_in <= {source}:
                continue
            # A name defined in more than one file does not say which
            # definition was called, and attributing it to every capability
            # holding that name manufactures verification. `main` is defined
            # in sixteen files here and `to_dict` in fourteen, so the
            # `turn_gate` capability was reported as exercised by
            # `tests/test_cli.py calls main` -- the CLI's main, not the
            # gate's. Three of its four references were that shape, and
            # before a real test existed the capability still read as
            # covered.
            #
            # This is the rule the route reader already follows for an
            # unresolved receiver: when the evidence does not distinguish,
            # make no claim either way.
            if len(defined_in) > 1:
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
                # Whole-file granularity means a reference from a source file
                # carrying inline tests proves the file reaches this, not that
                # a test does. Both are evidence and they are not equally
                # strong, so the sentence says which one it is instead of
                # letting a reader assume the stronger reading.
                (
                    f"{source} calls {_short_name(name)}"
                    if source in dedicated
                    else f"{source} contains tests and calls {_short_name(name)}"
                )
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
