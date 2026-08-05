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
MAX_TUNABLES = 120
MAX_SYMBOL_ROWS = 1_200
# Kinds that name something a reader can navigate to. `module` is excluded
# because the file census already lists every file, and a function-local binding
# is excluded from the readable index because it is not part of any module's
# surface — both remain complete in the JSON projection.
DECLARED_KINDS = frozenset(
    {
        "function",
        "async_function",
        "class",
        "struct",
        "enum",
        "trait",
        "interface",
        "type",
        "method",
        "property",
        "enum_member",
        "constant",
        "binding",
        "module_variable",
    }
)


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


def short_form(qualified_name: str) -> str:
    """The `module.name` spelling a developer uses when talking about a symbol.

    A fully qualified name carries the whole package path and any enclosing
    class: `backend.app.core.scaling_math.ScalingMath.get_xp_required`. That is
    unambiguous and nobody writes it. Imports, stack traces, review comments and
    prose all say `scaling_math.get_xp_required`, so a reader searching the
    document for the name they know finds nothing.

    Both spellings are correct, so the index carries both rather than choosing.
    """

    parts = qualified_name.split(".")
    if len(parts) < 2:
        return qualified_name
    leaf = parts[-1]
    # Walk back past enclosing classes to the module. A class is capitalised by
    # convention in every language this analyzes; a module is not. The result is
    # a heuristic, which is why it supplements the exact name instead of
    # replacing it.
    module = parts[-2]
    for candidate in reversed(parts[:-1]):
        if not candidate[:1].isupper():
            module = candidate
            break
    return f"{module}.{leaf}" if module != leaf else leaf


def _symbol_index(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Every extracted symbol, named. The ledger holds these; the document did not.

    A specification that discusses a module without ever naming its functions
    leaves a reader unable to search for them.
    """

    interesting = [item for item in symbols if str(item["kind"]) in DECLARED_KINDS]
    rows = tuple(
        (
            str(item["qualified_name"]),
            short_form(str(item["qualified_name"])),
            str(item["kind"]),
            f"{item['path']}:{item['start_line']}",
        )
        for item in sorted(interesting, key=lambda item: (item["path"], item["start_line"]))
    )
    note = (
        f"Showing {min(len(rows), MAX_SYMBOL_ROWS):,} of {len(rows):,} extracted "
        "symbols. Module-level variables and imports are omitted; the JSON "
        "projection carries the complete set. The short form elides the package "
        "path and any enclosing class so the name is searchable in the spelling "
        "imports and stack traces use; it is not guaranteed unique."
    )
    return Panel(
        name="symbol_index",
        title="Extracted symbol index",
        columns=("Symbol", "Short form", "Kind", "Defined at"),
        alignments=("left", "left", "left", "left"),
        rows=rows[:MAX_SYMBOL_ROWS],
        note=note,
    )


def _model_fields(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Annotated class attributes — the data contract declared outright.

    Where a repository persists JSON, this is the only place its schema is
    written down: not in the tables, which hold one blob column, and not in the
    symbol index, which holds functions.
    """

    rows: list[tuple[str, ...]] = []
    for symbol in symbols:
        models = symbol.get("metadata", {}).get("model_fields") or {}
        for model, entry in sorted(models.items()):
            bases = ", ".join(str(item) for item in entry.get("bases", ())) or "—"
            for field_entry in entry["fields"]:
                rows.append(
                    (
                        model,
                        bases,
                        str(field_entry["name"]),
                        f"`{field_entry['annotation']}`",
                        "required" if field_entry["required"] else "optional",
                        f"{symbol['path']}:{field_entry['line']}",
                    )
                )
    rows.sort(key=lambda row: (row[5], row[0]))
    return Panel(
        name="model_fields",
        title="Declared model fields",
        columns=("Model", "Base", "Field", "Annotation", "Requirement", "Declared at"),
        alignments=("left", "left", "left", "left", "left", "left"),
        rows=tuple(rows[:MAX_SYMBOL_ROWS]),
        note=(
            "Annotations are recorded as written, not resolved: the declared "
            "type is the contract, and what it evaluates to at import time is "
            "not knowable without running the code. A field is called required "
            "when the class body gives it no default, which is what the "
            "annotation states rather than what a validator may enforce."
        ),
    )


def _embedded_literals(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Numeric literals written straight into function bodies.

    A number with a module-level name is a tunable and the tunable index has
    it. A number written into the logic is the same decision made without a
    name, and it is the harder one to find: nothing indexes it, so changing
    the behaviour means locating every site by reading.
    """

    rows: list[tuple[str, ...]] = []
    for symbol in symbols:
        functions = symbol.get("metadata", {}).get("embedded_literals") or {}
        for name, entry in sorted(functions.items()):
            values = entry["values"]
            rendered = ", ".join(f"`{item['value']}`" for item in values[:12])
            if len(values) > 12:
                rendered += f" +{len(values) - 12:,} more"
            rows.append(
                (
                    name,
                    f"{len(values):,}",
                    rendered,
                    f"{symbol['path']}:{entry['line']}",
                )
            )
    rows.sort(key=lambda row: (-int(row[1].replace(",", "")), row[3], row[0]))
    return Panel(
        name="embedded_literals",
        title="Numeric literals inside functions",
        columns=("Function", "Distinct", "Values", "Defined at"),
        alignments=("left", "right", "left", "left"),
        rows=tuple(rows[:MAX_SYMBOL_ROWS]),
        note=(
            "0 and 1 are excluded because they are structural far more often "
            "than they are decisions. A value repeated within one function is "
            "counted once and located at its first site, so the count is of "
            "distinct values rather than of occurrences."
        ),
    )


def _string_constants(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Module-level string constants, with their values.

    The tunable index carries numbers because a number is obviously a dial. A
    string constant is one too: `_TELEGRAPH_ENRAGE = "ANNIHILATE"` names a
    value the rest of the system compares against, and a reader who cannot see
    it cannot match the constant to the data it meets at runtime.
    """

    rows: list[tuple[str, ...]] = []
    for symbol in symbols:
        constants = symbol.get("metadata", {}).get("string_constants") or {}
        for name, entry in sorted(constants.items()):
            rows.append(
                (
                    name,
                    f"`{entry['value']}`",
                    f"{symbol['path']}:{entry['line']}",
                )
            )
    rows.sort(key=lambda row: (row[2], row[0]))
    return Panel(
        name="string_constants",
        title="Declared string constants",
        columns=("Name", "Value", "Declared at"),
        alignments=("left", "left", "left"),
        rows=tuple(rows[:MAX_TUNABLES]),
        note=(
            "Module-level string assignments only. A value built at runtime, "
            "a multi-line string and a docstring are all excluded, so this is "
            "what the module states outright rather than what it computes."
        ),
    )


def _imported_names(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Which names each dependency actually contributes.

    An import edge says `fastapi` is used. It does not say that what is used is
    `Depends`, and that difference decides whether a dependency is load-bearing
    or incidental.
    """

    totals: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        imports = symbol.get("metadata", {}).get("imported_names") or {}
        for module, entry in imports.items():
            running = totals.setdefault(module, {"names": set(), "modules": 0})
            running["names"].update(str(item) for item in entry["names"])
            running["modules"] = int(running["modules"]) + 1
    rows = tuple(
        (
            module,
            f"{int(entry['modules']):,}",
            f"{len(entry['names']):,}",
            ", ".join(f"`{item}`" for item in sorted(entry["names"])[:10])
            + (f" +{len(entry['names']) - 10:,} more" if len(entry["names"]) > 10 else ""),
        )
        for module, entry in sorted(
            totals.items(), key=lambda item: (-len(item[1]["names"]), item[0])
        )
    )
    return Panel(
        name="imported_names",
        title="Names taken from each imported module",
        columns=("Module", "Importers", "Names", "Imported names"),
        alignments=("left", "right", "right", "left"),
        rows=rows[:MAX_SYMBOL_ROWS],
        note=(
            "Counted from import statements, so a name imported but never used "
            "still appears and a name reached dynamically does not. Aliases are "
            "recorded under the local name, which is how the importing module "
            "refers to it."
        ),
    )


def _payload_shapes(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Field names of the dictionaries functions return.

    Where a service serialises dictionaries instead of declaring models, the
    response contract is not written down anywhere: the storage schema sees a
    JSON blob and the symbol index sees a function. These are the names a
    caller has to code against, recovered from the returned literals.
    """

    rows: list[tuple[str, ...]] = []
    for symbol in symbols:
        shapes = symbol.get("metadata", {}).get("payload_shapes") or {}
        for name, entry in sorted(shapes.items()):
            fields = [str(item) for item in entry["fields"]]
            rows.append(
                (
                    name,
                    f"{len(fields):,}",
                    ", ".join(f"`{item}`" for item in fields[:12])
                    + (f" +{len(fields) - 12:,} more" if len(fields) > 12 else ""),
                    f"{symbol['path']}:{entry['line']}",
                )
            )
    rows.sort(key=lambda row: (row[3], row[0]))
    return Panel(
        name="payload_shapes",
        title="Returned payload fields",
        columns=("Function", "Fields", "Field names", "Returns at"),
        alignments=("left", "right", "left", "left"),
        rows=tuple(rows[:MAX_SYMBOL_ROWS]),
        note=(
            "Literal string keys of dictionaries returned by each function. A "
            "key computed at runtime is absent rather than guessed at, so this "
            "is a lower bound on the response shape, and a function returning "
            "several shapes contributes the union of them."
        ),
    )


def _external_origins(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Third-party hosts named in source string literals.

    A page that pulls a font from `fonts.googleapis.com` sends every visitor's
    address to a third party before any consent dialog renders. That is
    decided by a string in the source, not by anything in a dependency
    manifest, so nothing else in this analysis would surface it. Loopback
    addresses are excluded: those are the local process, not a third party.
    """

    totals: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        origins = symbol.get("metadata", {}).get("external_origins") or {}
        for host, entry in origins.items():
            running = totals.setdefault(
                host,
                {
                    "count": 0,
                    "scheme": str(entry["scheme"]),
                    "site": f"{symbol['path']}:{entry['first_line']}",
                },
            )
            running["count"] = int(running["count"]) + int(entry["count"])
    rows = tuple(
        (
            host,
            str(entry["scheme"]),
            f"{int(entry['count']):,}",
            str(entry["site"]),
        )
        for host, entry in sorted(
            totals.items(), key=lambda item: (-int(item[1]["count"]), item[0])
        )
    )
    return Panel(
        name="external_origins",
        title="Third-party origins in source literals",
        columns=("Host", "Scheme", "Literals", "First seen"),
        alignments=("left", "left", "right", "left"),
        rows=rows[:MAX_SYMBOL_ROWS],
        note=(
            "Hosts written as literals, not requests observed: a URL built at "
            "runtime from parts is absent, and a literal in dead code still "
            "appears. Loopback and .local hosts are excluded as local."
        ),
    )


def _external_api_surface(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Platform and library API the code reaches for but does not define.

    The symbol index answers "what does this repository declare". This answers
    "what does it depend on at runtime" — `localStorage`, `AbortController`,
    `EventSource`, an SDK's call chain. Those never appear in a symbol index
    because they are nobody's symbol here, yet browser storage is a privacy
    question and `eval` is a security one.
    """

    totals: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        references = symbol.get("metadata", {}).get("external_references") or {}
        for name, entry in references.items():
            running = totals.setdefault(
                name,
                {"count": 0, "called": False, "site": f"{symbol['path']}:{entry['first_line']}"},
            )
            running["count"] = int(running["count"]) + int(entry["count"])
            running["called"] = bool(running["called"]) or bool(entry.get("called"))
    rows = tuple(
        (
            name,
            "call" if entry["called"] else "access",
            f"{int(entry['count']):,}",
            str(entry["site"]),
        )
        for name, entry in sorted(
            totals.items(), key=lambda item: (-int(item[1]["count"]), item[0])
        )
    )
    return Panel(
        name="external_api_surface",
        title="Referenced external API",
        columns=("Reference", "Use", "Sites", "First seen"),
        alignments=("left", "left", "right", "left"),
        rows=rows[:MAX_SYMBOL_ROWS],
        note=(
            f"Showing {min(len(rows), MAX_SYMBOL_ROWS):,} of {len(rows):,} references. "
            "A name this repository declares is excluded, so what remains is "
            "reached from outside the module. Resolution is lexical: the "
            "receiver is the name as written, not the type it holds, so two "
            "different objects sharing a variable name are counted together."
        ),
    )


def _data_containers(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Module-level lookup tables with their sizes."""

    rows: list[tuple[str, ...]] = []
    for symbol in symbols:
        containers = symbol.get("metadata", {}).get("data_containers") or {}
        for name, entry in sorted(containers.items()):
            rows.append(
                (
                    name,
                    str(entry["kind"]),
                    f"{int(entry['size']):,}",
                    f"{symbol['path']}:{entry['line']}",
                )
            )
    rows.sort(key=lambda row: (row[3], row[0]))
    return Panel(
        name="data_containers",
        title="Module-level data tables",
        columns=("Name", "Kind", "Entries", "Defined at"),
        alignments=("left", "left", "right", "left"),
        rows=tuple(rows[:MAX_SYMBOL_ROWS]),
        note=(
            "Literal list, set, and dict assignments at module scope with two or "
            "more entries. A table built at import time by a call is not a literal "
            "and does not appear."
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
    if name == "symbol_index":
        return _symbol_index(context.symbols)
    if name == "model_fields":
        return _model_fields(context.symbols)
    if name == "embedded_literals":
        return _embedded_literals(context.symbols)
    if name == "string_constants":
        return _string_constants(context.symbols)
    if name == "imported_names":
        return _imported_names(context.symbols)
    if name == "payload_shapes":
        return _payload_shapes(context.symbols)
    if name == "external_origins":
        return _external_origins(context.symbols)
    if name == "external_api_surface":
        return _external_api_surface(context.symbols)
    if name == "data_containers":
        return _data_containers(context.symbols)
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
