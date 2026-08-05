# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Repository composition panels.

Panels report what the scanner actually saw: how many files, of what languages
and roles, how large, and — importantly — what was excluded and why. Exclusions
are reported rather than hidden, because a census that silently drops files
overstates its own coverage.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from open_skeleton.spec.capabilities import Capability
from open_skeleton.spec.consequences import Consequence

MAX_PANEL_ROWS = 15
MAX_CELL_ITEMS = 4
MAX_TUNABLES = 60


@dataclass(frozen=True, slots=True)
class PanelContext:
    """Everything a panel is allowed to read, all pinned to one snapshot."""

    files: tuple[dict[str, Any], ...] = ()
    exclusions: tuple[dict[str, Any], ...] = ()
    snapshot: dict[str, Any] = field(default_factory=dict)
    capabilities: tuple[Capability, ...] = ()
    consequences: tuple[Consequence, ...] = ()
    symbols: tuple[dict[str, Any], ...] = ()
    claim_locations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Panel:
    """A titled table of snapshot composition facts."""

    name: str
    title: str
    columns: tuple[str, ...]
    alignments: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "columns": list(self.columns),
            "rows": [list(row) for row in self.rows],
            "note": self.note,
        }


def _census(
    name: str,
    title: str,
    files: tuple[dict[str, Any], ...],
    field: str,
    label: str,
) -> Panel:
    counts: Counter[str] = Counter()
    lines: Counter[str] = Counter()
    size: Counter[str] = Counter()
    for item in files:
        key = str(item[field])
        counts[key] += 1
        lines[key] += int(item["line_count"])
        size[key] += int(item["size_bytes"])

    total_files = sum(counts.values()) or 1
    rows = tuple(
        (
            key,
            f"{counts[key]:,}",
            f"{counts[key] / total_files:.1%}",
            f"{lines[key]:,}",
            f"{size[key]:,}",
        )
        for key, _ in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    )
    return Panel(
        name=name,
        title=title,
        columns=(label, "Files", "Share", "Lines", "Bytes"),
        alignments=("left", "right", "right", "right", "right"),
        rows=rows,
    )


def _largest_files(files: tuple[dict[str, Any], ...]) -> Panel:
    ranked = sorted(files, key=lambda item: -int(item["line_count"]))[:MAX_PANEL_ROWS]
    rows = tuple(
        (
            str(item["path"]),
            str(item["language"]),
            str(item["role"]),
            f"{int(item['line_count']):,}",
            str(item["sha256"])[:12],
        )
        for item in ranked
    )
    note = (
        f"Showing the {len(ranked):,} largest of {len(files):,} included files."
        if len(files) > len(ranked)
        else None
    )
    return Panel(
        name="largest_files",
        title="Largest included files",
        columns=("Path", "Language", "Role", "Lines", "SHA-256"),
        alignments=("left", "left", "left", "right", "left"),
        rows=rows,
        note=note,
    )


def _exclusions(exclusions: tuple[dict[str, Any], ...]) -> Panel:
    counts = Counter(str(item["reason"]) for item in exclusions)
    rows = tuple(
        (reason, f"{count:,}")
        for reason, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    )
    return Panel(
        name="exclusions",
        title="Excluded entries by reason",
        columns=("Exclusion reason", "Entries"),
        alignments=("left", "right"),
        rows=rows,
        note=(
            "Excluded content was never read into the analysis. Coverage percentages "
            "elsewhere in this document are relative to included files only."
        ),
    )


def _snapshot_totals(
    files: tuple[dict[str, Any], ...],
    exclusions: tuple[dict[str, Any], ...],
    snapshot: dict[str, Any],
) -> Panel:
    languages = {str(item["language"]) for item in files}
    roles = {str(item["role"]) for item in files}
    rows = (
        ("Included files", f"{len(files):,}"),
        ("Included lines", f"{sum(int(item['line_count']) for item in files):,}"),
        ("Included bytes", f"{sum(int(item['size_bytes']) for item in files):,}"),
        ("Distinct languages", f"{len(languages):,}"),
        ("Distinct roles", f"{len(roles):,}"),
        ("Excluded entries", f"{len(exclusions):,}"),
        ("Scan duration", f"{int(snapshot.get('duration_ms', 0)):,} ms"),
        ("Policy version", str(snapshot.get("policy_version", "unknown"))),
    )
    return Panel(
        name="snapshot_totals",
        title="Snapshot totals",
        columns=("Measure", "Value"),
        alignments=("left", "right"),
        rows=rows,
    )


def _truncate(items: tuple[str, ...]) -> str:
    if not items:
        return "—"
    shown = ", ".join(f"`{item}`" for item in items[:MAX_CELL_ITEMS])
    remaining = len(items) - MAX_CELL_ITEMS
    return f"{shown} +{remaining:,} more" if remaining > 0 else shown


def _capability_catalog(capabilities: tuple[Capability, ...]) -> Panel:
    rows = tuple(
        (
            item.capability_id,
            item.label,
            item.kind,
            f"{len(item.routes):,}",
            f"{len(item.symbols):,}",
            item.verification,
        )
        for item in capabilities
    )
    return Panel(
        name="capability_catalog",
        title="Implemented capability catalog",
        columns=("ID", "Capability", "Cluster", "Routes", "Symbols", "Verification"),
        alignments=("left", "left", "left", "right", "right", "left"),
        rows=rows,
        note=(
            "Capabilities are clustered from served route prefixes and source "
            "module structure. They describe what the implementation exposes, "
            "not what anyone required it to do."
        ),
    )


def _traceability_matrix(capabilities: tuple[Capability, ...]) -> Panel:
    rows = tuple(
        (
            item.capability_id,
            _truncate(item.routes) if item.routes else _truncate(item.symbols),
            _truncate(item.paths),
            f"{len(item.evidence_ids):,}",
            _truncate(item.exercised_by),
        )
        for item in capabilities
    )
    return Panel(
        name="traceability_matrix",
        title="Capability traceability",
        columns=("ID", "Surface", "Implementing files", "Receipts", "Exercised by"),
        alignments=("left", "left", "left", "right", "left"),
        rows=rows,
        note=(
            "The exercising column follows call edges out of test-role files and "
            "operator-harness scripts into each capability's symbols. A reference "
            "proves the symbol is called from a verifying file; it does not prove "
            "the assertion is meaningful."
        ),
    )


def _verification_gaps(capabilities: tuple[Capability, ...]) -> Panel:
    gaps = [item for item in capabilities if not item.exercised_by]
    rows = tuple(
        (item.capability_id, item.label, item.kind, _truncate(item.paths)) for item in gaps
    )
    covered = len(capabilities) - len(gaps)
    total = len(capabilities) or 1
    return Panel(
        name="verification_gaps",
        title="Capabilities with no verifying reference",
        columns=("ID", "Capability", "Cluster", "Implementing files"),
        alignments=("left", "left", "left", "left"),
        rows=rows,
        note=(
            f"{covered:,} of {len(capabilities):,} capabilities "
            f"({covered / total:.0%}) are reached from a test-role file or an "
            "operator-harness script. The rows above are the remainder."
        ),
    )


def _consequences(
    consequences: tuple[Consequence, ...],
    locations: dict[str, str],
) -> Panel:
    # A consequence a reader cannot locate is worth less than one they can, so
    # the derivation shows where its claims live rather than only how many.
    rows = tuple(
        (
            item.severity,
            item.statement,
            _truncate(
                tuple(
                    dict.fromkeys(
                        locations[claim_id] for claim_id in item.claim_ids if claim_id in locations
                    )
                )
            ),
        )
        for item in consequences
    )
    return Panel(
        name="consequences",
        title="What these findings imply together",
        columns=("Severity", "Consequence", "Evidence"),
        alignments=("left", "left", "left"),
        rows=rows,
        note=(
            "Each row composes claims already established above; no row asserts "
            "anything the cited claims do not. A consequence follows from the "
            "combination, so it holds exactly as far as those claims do."
        ),
    )


def _tunable_index(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Every module-level numeric constant, with its value and definition line.

    These are the numbers a maintainer changes to alter behaviour without
    changing logic, and they are otherwise scattered across the source.
    """

    rows: list[tuple[str, ...]] = []
    for symbol in symbols:
        tunables = symbol.get("metadata", {}).get("tunables") or {}
        for name, entry in sorted(tunables.items()):
            value = entry["value"]
            rendered = f"{value:g}" if isinstance(value, (int, float)) else str(value)
            rows.append((name, rendered, f"{symbol['path']}:{entry['line']}"))
    rows.sort(key=lambda row: (row[2], row[0]))
    return Panel(
        name="tunable_index",
        title="Numeric tunables",
        columns=("Constant", "Value", "Defined at"),
        alignments=("left", "right", "left"),
        rows=tuple(rows[:MAX_TUNABLES]),
        note=(
            "Module-level numeric assignments only. A value computed at import "
            "time or read from configuration is not a literal and does not appear."
        ),
    )


def _failure_surface(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Every raise a route handler can reach, grouped by status.

    A caller needs to know which failures a route can produce. That is recorded
    in each handler's guard trace; this consolidates it into one surface.
    """

    seen: dict[tuple[str, str, str], None] = {}
    for symbol in symbols:
        flow = symbol.get("metadata", {}).get("control_flow") or []
        routes = symbol.get("metadata", {}).get("routes") or []
        if not routes:
            continue
        surface = ", ".join(
            f"{item.get('method', '?')} {item.get('path', '?')}" for item in routes[:2]
        )
        for event in flow:
            if event.get("kind") != "raise":
                continue
            key = (
                str(event["label"]),
                surface,
                f"{symbol['path']}:{event['line']}",
            )
            seen.setdefault(key, None)
    rows = tuple(sorted(seen, key=lambda row: (row[0], row[2])))
    return Panel(
        name="failure_surface",
        title="Failure responses reachable from a route",
        columns=("Raised", "Route", "At"),
        alignments=("left", "left", "left"),
        rows=rows[: MAX_PANEL_ROWS * 3],
        note=(
            "Raises recorded inside route handler bodies. A failure produced by "
            "framework validation, by middleware, or by a helper the handler "
            "calls is not counted here."
        ),
    )


def build_panel(name: str, context: PanelContext) -> Panel:
    """Render one named panel from pinned snapshot records."""

    if name == "language_census":
        return _census(
            "language_census", "Composition by language", context.files, "language", "Language"
        )
    if name == "role_census":
        return _census("role_census", "Composition by role", context.files, "role", "Role")
    if name == "largest_files":
        return _largest_files(context.files)
    if name == "exclusions":
        return _exclusions(context.exclusions)
    if name == "snapshot_totals":
        return _snapshot_totals(context.files, context.exclusions, context.snapshot)
    if name == "capability_catalog":
        return _capability_catalog(context.capabilities)
    if name == "traceability_matrix":
        return _traceability_matrix(context.capabilities)
    if name == "tunable_index":
        return _tunable_index(context.symbols)
    if name == "failure_surface":
        return _failure_surface(context.symbols)
    if name == "consequences":
        return _consequences(context.consequences, context.claim_locations)
    if name == "verification_gaps":
        return _verification_gaps(context.capabilities)
    return Panel(  # pragma: no cover - profile validation rejects unknown panels
        name=name,
        title=name,
        columns=("Measure", "Value"),
        alignments=("left", "left"),
        rows=(),
        note=f"No generator is registered for panel '{name}'.",
    )
