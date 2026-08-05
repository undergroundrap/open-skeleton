# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROFILE_SCHEMA_VERSION = "open-skeleton.spec_profile.v1"

PROBE_KINDS = frozenset(
    {
        "path_glob",
        "file_language",
        "file_role",
        "claim_category",
        "sourced_claim_category",
        "symbol_kind",
        "edge_relationship",
        "dependency_name",
        "import_target",
    }
)

PANEL_KINDS = frozenset(
    {
        "language_census",
        "role_census",
        "largest_files",
        "exclusions",
        "snapshot_totals",
        "capability_catalog",
        "traceability_matrix",
        "verification_gaps",
        "consequences",
        "tunable_index",
        "failure_surface",
        "symbol_index",
        "data_containers",
    }
)

VERDICTS = ("applicable", "degenerate", "absent", "structural")

_IMPORTANCE_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class ProfileError(ValueError):
    """Raised when a spec profile is structurally invalid."""


@dataclass(frozen=True, slots=True)
class SpecProbe:
    """One named, re-runnable query that decides whether a concern is present."""

    name: str
    kind: str
    terms: tuple[str, ...]

    @property
    def query_display(self) -> str:
        return f"{self.kind}: {', '.join(self.terms)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "terms": list(self.terms),
            "query": self.query_display,
        }


@dataclass(frozen=True, slots=True)
class SpecSelector:
    """Selects existing ledger claims into an outline node."""

    categories: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    min_importance: str = "low"
    limit: int = 40

    def accepts(self, claim: dict[str, Any]) -> bool:
        if self.categories and claim["category"] not in self.categories:
            return False
        if self.statuses and claim["status"] not in self.statuses:
            return False
        threshold = _IMPORTANCE_ORDER[self.min_importance]
        return _IMPORTANCE_ORDER.get(str(claim["importance"]), 3) <= threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "categories": list(self.categories),
            "statuses": list(self.statuses),
            "min_importance": self.min_importance,
            "limit": self.limit,
        }


@dataclass(frozen=True, slots=True)
class SpecSection:
    """One outline node: a concern, how to detect it, and what to say about it."""

    section_id: str
    number: str
    title: str
    concern: str
    framing: str = ""
    probes: tuple[SpecProbe, ...] = ()
    findings: SpecSelector | None = None
    constraints: SpecSelector | None = None
    diagrams: tuple[str, ...] = ()
    panels: tuple[str, ...] = ()
    cross_references: tuple[str, ...] = ()
    degenerate_below: int = 0
    children: tuple[SpecSection, ...] = ()

    def walk(self) -> list[SpecSection]:
        nodes = [self]
        for child in self.children:
            nodes.extend(child.walk())
        return nodes


@dataclass(frozen=True, slots=True)
class SpecProfile:
    """A complete, user-editable outline definition."""

    profile_id: str
    profile_version: str
    title: str
    lineage: str
    sections: tuple[SpecSection, ...] = field(default_factory=tuple)

    @property
    def qualified_id(self) -> str:
        return f"{self.profile_id}/{self.profile_version}"

    def walk(self) -> list[SpecSection]:
        nodes: list[SpecSection] = []
        for section in self.sections:
            nodes.extend(section.walk())
        return nodes


def _require(payload: dict[str, Any], key: str, context: str) -> Any:
    if key not in payload:
        raise ProfileError(f"{context}: missing required key '{key}'")
    return payload[key]


def _string_tuple(value: Any, context: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProfileError(f"{context}: expected a list of strings")
    return tuple(value)


def _parse_probe(payload: dict[str, Any], context: str) -> SpecProbe:
    name = str(_require(payload, "name", context))
    kind = str(_require(payload, "kind", context))
    if kind not in PROBE_KINDS:
        raise ProfileError(
            f"{context}: unsupported probe kind '{kind}'; expected one of {sorted(PROBE_KINDS)}"
        )
    terms = _string_tuple(_require(payload, "terms", context), context)
    if not terms:
        raise ProfileError(f"{context}: probe '{name}' declares no terms")
    return SpecProbe(name=name, kind=kind, terms=terms)


def _parse_selector(payload: Any, context: str) -> SpecSelector | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ProfileError(f"{context}: selector must be an object")
    min_importance = str(payload.get("min_importance", "low"))
    if min_importance not in _IMPORTANCE_ORDER:
        raise ProfileError(f"{context}: min_importance must be one of {sorted(_IMPORTANCE_ORDER)}")
    limit = int(payload.get("limit", 40))
    if limit < 1 or limit > 500:
        raise ProfileError(f"{context}: selector limit must be between 1 and 500")
    return SpecSelector(
        categories=_string_tuple(payload.get("categories"), context),
        statuses=_string_tuple(payload.get("statuses"), context),
        min_importance=min_importance,
        limit=limit,
    )


def _parse_section(payload: dict[str, Any], context: str, seen: set[str]) -> SpecSection:
    section_id = str(_require(payload, "id", context))
    if section_id in seen:
        raise ProfileError(f"{context}: duplicate section id '{section_id}'")
    seen.add(section_id)
    node_context = f"section '{section_id}'"

    probes = tuple(_parse_probe(item, node_context) for item in payload.get("probes", []))
    degenerate_below = int(payload.get("degenerate_below", 0))
    if degenerate_below < 0:
        raise ProfileError(f"{node_context}: degenerate_below must not be negative")

    panels = _string_tuple(payload.get("panels"), node_context)
    for panel in panels:
        if panel not in PANEL_KINDS:
            raise ProfileError(
                f"{node_context}: unsupported panel '{panel}'; "
                f"expected one of {sorted(PANEL_KINDS)}"
            )

    children = tuple(
        _parse_section(item, node_context, seen) for item in payload.get("children", [])
    )

    return SpecSection(
        section_id=section_id,
        number=str(_require(payload, "number", node_context)),
        title=str(_require(payload, "title", node_context)),
        concern=str(payload.get("concern", "")),
        framing=str(payload.get("framing", "")),
        probes=probes,
        findings=_parse_selector(payload.get("findings"), node_context),
        constraints=_parse_selector(payload.get("constraints"), node_context),
        diagrams=_string_tuple(payload.get("diagrams"), node_context),
        panels=panels,
        cross_references=_string_tuple(payload.get("cross_references"), node_context),
        degenerate_below=degenerate_below,
        children=children,
    )


def parse_profile(payload: dict[str, Any]) -> SpecProfile:
    """Validate and build a profile from a decoded JSON document."""

    schema = str(_require(payload, "schema", "profile"))
    if schema != PROFILE_SCHEMA_VERSION:
        raise ProfileError(
            f"profile: unsupported schema '{schema}'; expected '{PROFILE_SCHEMA_VERSION}'"
        )
    raw_sections = _require(payload, "sections", "profile")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ProfileError("profile: 'sections' must be a non-empty list")

    seen: set[str] = set()
    sections = tuple(_parse_section(item, "profile", seen) for item in raw_sections)

    profile = SpecProfile(
        profile_id=str(_require(payload, "profile_id", "profile")),
        profile_version=str(_require(payload, "profile_version", "profile")),
        title=str(_require(payload, "title", "profile")),
        lineage=str(payload.get("lineage", "")),
        sections=sections,
    )

    known = {section.section_id for section in profile.walk()}
    for section in profile.walk():
        for reference in section.cross_references:
            if reference not in known:
                raise ProfileError(
                    f"section '{section.section_id}': cross reference "
                    f"'{reference}' does not name a section in this profile"
                )
    return profile


def default_profile_path() -> Path:
    """Return the packaged standard profile."""

    return Path(__file__).resolve().parent / "profiles" / "standard.json"


def load_profile(path: Path | None = None) -> SpecProfile:
    """Load a profile from disk, defaulting to the packaged standard outline."""

    resolved = (path or default_profile_path()).expanduser().resolve(strict=True)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"{resolved}: invalid JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise ProfileError(f"{resolved}: profile must be a JSON object")
    return parse_profile(payload)
