# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any

from open_skeleton.spec.profile import SpecProbe, SpecSection

MAX_RECORDED_MATCHES = 12


@dataclass(frozen=True, slots=True)
class LedgerCorpus:
    """The pinned snapshot facts a probe may query. Nothing else is readable."""

    snapshot_id: str
    files: tuple[dict[str, Any], ...]
    claims: tuple[dict[str, Any], ...]
    symbols: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...] = ()

    def sourced_evidence_ids(self) -> frozenset[str]:
        """Receipts that point at a real file, not a repository-wide census.

        Analyzers emit census receipts with the synthetic path ``.`` to record a
        counted absence. Those must never make a concern look present, so presence
        probes filter on this set.
        """

        return frozenset(
            str(item["evidence_id"])
            for item in self.evidence
            if str(item["path"]) not in {".", ""} and not str(item["path"]).startswith("@")
        )


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The outcome of one probe, including the query text that produced it."""

    name: str
    kind: str
    query: str
    match_count: int
    matches: tuple[str, ...]
    # The individual things looked for. Carried apart from `query` because a
    # reader learning what is absent needs the artifacts named one at a time:
    # "no `Dockerfile`, `docker-compose.yml` or `Containerfile`" says what
    # `path_glob: Dockerfile, docker-compose.yml, Containerfile` does not.
    terms: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "query": self.query,
            "match_count": self.match_count,
            "matches": list(self.matches),
            "terms": list(self.terms),
        }


def _match_path_glob(corpus: LedgerCorpus, terms: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for item in corpus.files:
        path = str(item["path"])
        basename = path.rsplit("/", 1)[-1]
        for pattern in terms:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern):
                matches.append(path)
                break
    return matches


def _match_file_field(corpus: LedgerCorpus, terms: tuple[str, ...], field: str) -> list[str]:
    wanted = {term.casefold() for term in terms}
    return [str(item["path"]) for item in corpus.files if str(item[field]).casefold() in wanted]


def _match_claim_category(corpus: LedgerCorpus, terms: tuple[str, ...]) -> list[str]:
    wanted = set(terms)
    return [str(item["claim_id"]) for item in corpus.claims if str(item["category"]) in wanted]


def _match_sourced_claim_category(corpus: LedgerCorpus, terms: tuple[str, ...]) -> list[str]:
    wanted = set(terms)
    sourced = corpus.sourced_evidence_ids()
    return [
        str(item["claim_id"])
        for item in corpus.claims
        if str(item["category"]) in wanted
        and any(evidence_id in sourced for evidence_id in item.get("supporting_evidence", ()))
    ]


def _match_symbol_kind(corpus: LedgerCorpus, terms: tuple[str, ...]) -> list[str]:
    wanted = set(terms)
    return [
        f"{item['path']}::{item['qualified_name']}"
        for item in corpus.symbols
        if str(item["kind"]) in wanted
    ]


def _match_edge_relationship(corpus: LedgerCorpus, terms: tuple[str, ...]) -> list[str]:
    wanted = set(terms)
    return [
        f"{item['source_path']} -> {item['target_ref']}"
        for item in corpus.edges
        if str(item["relationship"]) in wanted
    ]


def _match_edge_target(
    corpus: LedgerCorpus, terms: tuple[str, ...], relationship: str
) -> list[str]:
    """Glob-match the target of one relationship, case-insensitively.

    Package and module names are written several ways for the same library, so a
    pattern is tried against every reasonable spelling of the target:

    - the target itself (``@opentelemetry/api``)
    - with an npm scope marker stripped (``opentelemetry/api``)
    - the final path segment (``api``)
    - the leading dotted segment, for module paths (``opentelemetry`` from
      ``opentelemetry.trace``)

    This keeps profile terms readable without making them match across
    relationships: a ``dependency_name`` probe never sees an import edge.
    """

    matches: list[str] = []
    patterns = [term.casefold() for term in terms]
    for item in corpus.edges:
        if str(item["relationship"]) != relationship:
            continue
        target = str(item["target_ref"])
        folded = target.casefold()
        candidates = {
            folded,
            folded.lstrip("@"),
            folded.rsplit("/", 1)[-1],
            folded.split(".", 1)[0],
        }
        if any(
            fnmatch.fnmatch(candidate, pattern) for candidate in candidates for pattern in patterns
        ):
            matches.append(target)
    return matches


def run_probe(probe: SpecProbe, corpus: LedgerCorpus) -> ProbeResult:
    """Execute one probe against the pinned snapshot."""

    if probe.kind == "path_glob":
        matches = _match_path_glob(corpus, probe.terms)
    elif probe.kind == "file_language":
        matches = _match_file_field(corpus, probe.terms, "language")
    elif probe.kind == "file_role":
        matches = _match_file_field(corpus, probe.terms, "role")
    elif probe.kind == "claim_category":
        matches = _match_claim_category(corpus, probe.terms)
    elif probe.kind == "sourced_claim_category":
        matches = _match_sourced_claim_category(corpus, probe.terms)
    elif probe.kind == "symbol_kind":
        matches = _match_symbol_kind(corpus, probe.terms)
    elif probe.kind == "edge_relationship":
        matches = _match_edge_relationship(corpus, probe.terms)
    elif probe.kind == "dependency_name":
        matches = _match_edge_target(corpus, probe.terms, "declares_dependency")
    elif probe.kind == "import_target":
        matches = _match_edge_target(corpus, probe.terms, "imports")
    else:  # pragma: no cover - profile validation rejects unknown kinds
        raise ValueError(f"Unsupported probe kind: {probe.kind}")

    unique = sorted(dict.fromkeys(matches))
    return ProbeResult(
        name=probe.name,
        kind=probe.kind,
        query=probe.query_display,
        match_count=len(unique),
        matches=tuple(unique[:MAX_RECORDED_MATCHES]),
        terms=tuple(probe.terms),
    )


def evaluate_section(
    section: SpecSection, corpus: LedgerCorpus
) -> tuple[str, tuple[ProbeResult, ...]]:
    """Return the applicability verdict for one outline node and its probe evidence.

    A node with no probes is ``structural``: it organizes other nodes and makes no
    presence claim of its own. Otherwise the verdict is decided only by counted
    matches, so it is reproducible from the snapshot alone.
    """

    if not section.probes:
        return "structural", ()

    results = tuple(run_probe(probe, corpus) for probe in section.probes)
    total = sum(result.match_count for result in results)
    if total == 0:
        return "absent", results
    if section.degenerate_below and total < section.degenerate_below:
        return "degenerate", results
    return "applicable", results
