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
from dataclasses import dataclass
from typing import Any

MAX_PANEL_ROWS = 15


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


def build_panel(
    name: str,
    *,
    files: tuple[dict[str, Any], ...],
    exclusions: tuple[dict[str, Any], ...],
    snapshot: dict[str, Any],
) -> Panel:
    """Render one named composition panel from pinned snapshot records."""

    if name == "language_census":
        return _census("language_census", "Composition by language", files, "language", "Language")
    if name == "role_census":
        return _census("role_census", "Composition by role", files, "role", "Role")
    if name == "largest_files":
        return _largest_files(files)
    if name == "exclusions":
        return _exclusions(exclusions)
    if name == "snapshot_totals":
        return _snapshot_totals(files, exclusions, snapshot)
    return Panel(  # pragma: no cover - profile validation rejects unknown panels
        name=name,
        title=name,
        columns=("Measure", "Value"),
        alignments=("left", "left"),
        rows=(),
        note=f"No generator is registered for panel '{name}'.",
    )
