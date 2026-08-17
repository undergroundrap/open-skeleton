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
from open_skeleton.spec.roles import MultiRole
from open_skeleton.spec.substitutes import Substitute

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
    substitutes: tuple[Substitute, ...] = ()
    section_verdicts: dict[str, str] = field(default_factory=dict)
    claims_by_category: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)
    roles: tuple[MultiRole, ...] = ()


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
            # An analyzer may know a constant's name and site without its value:
            # a Rust `static` can be declared in one place and assigned in
            # another. A missing optional field must not take the whole
            # document down, which is the contract every contributed analyzer
            # gets to rely on.
            value = entry.get("value", "—")
            rendered = f"{value:g}" if isinstance(value, (int, float)) else str(value)
            rows.append((name, rendered, f"{symbol['path']}:{entry.get('line', 1)}"))
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
            for field_entry in entry.get("fields", ()):
                annotation = field_entry.get("annotation")
                requirement = field_entry.get("required")
                rows.append(
                    (
                        model,
                        bases,
                        str(field_entry.get("name", "—")),
                        f"`{annotation}`" if annotation else "—",
                        "required"
                        if requirement
                        else ("optional" if requirement is False else "—"),
                        f"{symbol['path']}:{field_entry.get('line', 1)}",
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


def _signatures(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Function parameters, with the annotation and default as written.

    Whether a parameter is required, what type it declares and what it falls
    back to are the three things that decide how a function is called. The
    symbol index carried the name and nothing else.
    """

    rows: list[tuple[str, ...]] = []
    for symbol in symbols:
        signatures = symbol.get("metadata", {}).get("signatures") or {}
        for name, entry in sorted(signatures.items()):
            rendered: list[str] = []
            for parameter in entry["parameters"]:
                text = str(parameter["name"])
                if parameter.get("kind") == "var_positional":
                    text = f"*{text}"
                elif parameter.get("kind") == "var_keyword":
                    text = f"**{text}"
                if parameter.get("annotation"):
                    text += f": {parameter['annotation']}"
                if "default" in parameter:
                    text += f" = {parameter['default']}"
                rendered.append(text)
            required = sum(
                1
                for parameter in entry["parameters"]
                if "default" not in parameter
                and parameter.get("kind") not in {"var_positional", "var_keyword"}
            )
            rows.append(
                (
                    name,
                    f"{len(entry['parameters']):,}",
                    f"{required:,}",
                    f"`({', '.join(rendered)})`",
                    str(entry["returns"] or "—"),
                    f"{symbol['path']}:{entry['line']}",
                )
            )
    rows.sort(key=lambda row: (row[5], row[0]))
    return Panel(
        name="signatures",
        title="Function signatures",
        columns=("Function", "Params", "Required", "Signature", "Returns", "Defined at"),
        alignments=("left", "right", "right", "left", "left", "left"),
        rows=tuple(rows[:MAX_SYMBOL_ROWS]),
        note=(
            "Annotations and defaults are rendered from source rather than "
            "evaluated, so a mutable default stays visible as the expression "
            "it is. A parameter counts as required when it declares no "
            "default; *args and **kwargs are neither required nor optional "
            "and are excluded from that count."
        ),
    )


def _object_keys(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Field names coined as object literal keys.

    A request body assembled inline is a contract with the server that exists
    only as these keys: there is no model class to read, and the symbol index
    holds the function that builds the object rather than its shape. This is
    the client-side counterpart to the returned payload fields.
    """

    totals: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        keys = symbol.get("metadata", {}).get("object_keys") or {}
        for name, entry in keys.items():
            running = totals.setdefault(
                name,
                {"count": 0, "site": f"{symbol['path']}:{entry['first_line']}"},
            )
            running["count"] = int(running["count"]) + int(entry["count"])
    rows = tuple(
        (name, f"{int(entry['count']):,}", str(entry["site"]))
        for name, entry in sorted(
            totals.items(), key=lambda item: (-int(item[1]["count"]), item[0])
        )
    )
    return Panel(
        name="object_keys",
        title="Object literal field names",
        columns=("Field", "Sites", "First seen"),
        alignments=("left", "right", "left"),
        rows=rows[:MAX_SYMBOL_ROWS],
        note=(
            "Keys written literally in object position. A computed key is "
            "absent rather than guessed at, and shorthand is counted only "
            "where the name is declared in the same module, so this is a "
            "lower bound on the shapes the code builds."
        ),
    )


# Each row is a control a security reviewer expects to find, the profile
# section that probes for it, and the claim categories that evidence it. The
# matrix asserts nothing itself: it consolidates verdicts that already exist,
# because a reviewer should not have to read nine sections to answer one
# question.
SECURITY_CONTROLS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Request authentication", "security.authentication", ("auth_control", "auth_control_census")),
    ("Transport and origin boundary", "security.boundaries", ("security_boundary",)),
    ("Credential and token handling", "security.session-handling", ("session_control",)),
    ("Delegated identity", "security.identity-provider", ()),
    ("Cryptography in use", "security.data-protection", ("data_protection",)),
    ("Secret management", "integration.secrets", ()),
    ("Dependency vulnerability scanning", "security.vulnerability-management", ()),
    ("Third-party data egress", "security.third-party-origins", ("third_party_origin",)),
    ("Abuse and rate control", "integration.request-throttling", ("rate_limit",)),
    ("Audit and compliance artifacts", "operations.audit-trail", ()),
    ("Memory safety surface", "security.memory-safety", ("unsafe_surface",)),
    ("Governance artifacts", "security.governance", ()),
)


def _data_flow(context: PanelContext) -> Panel:
    """Where data enters each module, where it rests, and where it leaves.

    A dependency diagram shows which modules reference which. It does not show
    which of them are reachable from outside, which write something that
    outlives the request, and which send bytes to another host — the three
    facts a reader needs to answer "if this data is sensitive, where does it
    go".

    Granularity is the module, deliberately. Call edges here record a module
    and a called name rather than a resolved function, so a claim about which
    route reaches which table would be a guess dressed as a trace. What a
    module does is knowable; which of its handlers did it is not, and the note
    says so.
    """

    modules: dict[str, dict[str, Any]] = {}

    def entry(name: str, path: str) -> dict[str, Any]:
        return modules.setdefault(
            name,
            {"path": path, "routes": 0, "stores": set(), "hosts": set(), "state": 0, "imports": 0},
        )

    for symbol in context.symbols:
        metadata = symbol.get("metadata") or {}
        name = str(symbol.get("qualified_name", ""))
        path = str(symbol.get("path", ""))
        if not name:
            continue
        if metadata.get("routes"):
            record = entry(name.rsplit(".", 1)[0] if "." in name else name, path)
            record["routes"] += len(metadata["routes"])
        if metadata.get("external_origins"):
            entry(name, path)["hosts"].update(metadata["external_origins"])
        if metadata.get("imported_names"):
            entry(name, path)["imports"] += len(metadata["imported_names"])

    for category, key in (("storage_schema", "stores"), ("process_local_state", "state")):
        for claim in context.claims_by_category.get(category, ()):
            location = context.claim_locations.get(str(claim.get("claim_id", "")), "")
            text = str(claim.get("claim", ""))
            owner = text.split(" ", 1)[0].rsplit(".", 1)[0]
            if not owner:
                continue
            record = entry(owner, location.split(":")[0] if location else "")
            if key == "stores":
                table = text.rsplit("table ", 1)[-1].rstrip(".") if "table " in text else ""
                if table:
                    record["stores"].add(table)
            else:
                record["state"] = int(record["state"]) + 1

    rows = tuple(
        (
            name,
            f"{record['routes']:,}" if record["routes"] else "—",
            ", ".join(sorted(record["stores"])) or "—",
            f"{record['state']:,}" if record["state"] else "—",
            ", ".join(sorted(record["hosts"])) or "—",
            f"{record['imports']:,}" if record["imports"] else "—",
        )
        for name, record in sorted(
            modules.items(),
            key=lambda item: (
                -item[1]["routes"],
                -len(item[1]["stores"]),
                -len(item[1]["hosts"]),
                item[0],
            ),
        )
        if record["routes"] or record["stores"] or record["hosts"] or record["state"]
    )
    return Panel(
        name="data_flow",
        title="Where data enters, rests, and leaves each module",
        columns=(
            "Module or type",
            "Routes served",
            "Durable tables",
            "Process-local state",
            "External hosts",
            "Imported modules",
        ),
        alignments=("left", "right", "left", "right", "left", "right"),
        rows=rows[:MAX_SYMBOL_ROWS],
        note=(
            "Granularity is the module on purpose. Call edges record a module "
            "and a called name rather than a resolved function, so naming which "
            "route reaches which table would be a guess presented as a trace. "
            "A row listed with both a route and a table is one where those "
            "two facts are true together, not one where a path from the first "
            "to the second has been demonstrated. Storage is attributed to the "
            "owner named in the claim, which is a class where a class owns the "
            "connection."
        ),
    )


def _endpoint_catalog(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Every served route with what its own handler does before answering.

    The route census says a path exists. It does not say what the handler
    checks first, how it can refuse, or what comes back — which is everything
    a caller needs and everything an operator asks about during an incident.
    All three already sit on the handler symbol; nothing here is derived
    beyond joining them.
    """

    # Payload shapes are recorded against the module that declares the
    # function, not against the handler symbol, so the join is by function
    # name rather than by symbol identity.
    shapes_by_function: dict[str, list[str]] = {}
    for symbol in symbols:
        for name, entry in ((symbol.get("metadata") or {}).get("payload_shapes") or {}).items():
            shapes_by_function.setdefault(str(name), []).extend(
                str(item) for item in entry.get("fields", ())
            )

    rows: list[tuple[str, ...]] = []
    for symbol in symbols:
        metadata = symbol.get("metadata") or {}
        routes = metadata.get("routes") or []
        if not routes:
            continue
        flow = metadata.get("control_flow") or []
        guards = sum(1 for event in flow if event.get("kind") == "guard")
        refusals = sorted(
            {
                str(event.get("label", ""))
                for event in flow
                if event.get("kind") == "raise" and str(event.get("label", "")).startswith("HTTP")
            }
        )
        handler = str(symbol.get("qualified_name", ""))
        fields = shapes_by_function.get(handler.rsplit(".", 1)[-1], [])
        for route in routes:
            if str(route.get("role", "")) == "test":
                # Registered to exercise the framework, not to serve traffic.
                continue
            rows.append(
                (
                    str(route.get("method", "")),
                    f"`{route.get('path', '')}`",
                    short_form(handler),
                    f"{guards:,}",
                    ", ".join(refusals) or "—",
                    ", ".join(f"`{item}`" for item in sorted(set(fields))[:6]) or "—",
                    f"{symbol['path']}:{route.get('start_line', 1)}",
                )
            )
    rows.sort(key=lambda row: (row[1], row[0]))
    unguarded = sum(1 for row in rows if row[3] == "0")
    return Panel(
        name="endpoint_catalog",
        title="Endpoint catalog and response conventions",
        columns=(
            "Method",
            "Path",
            "Handler",
            "Guards",
            "Refuses with",
            "Response fields",
            "Declared at",
        ),
        alignments=("left", "left", "left", "right", "left", "left", "left"),
        rows=tuple(rows[:MAX_SYMBOL_ROWS]),
        note=(
            f"{len(rows):,} served routes, of which {unguarded:,} reach their body with no guard "
            "recorded in the handler itself. Guards and refusals are counted inside the "
            "handler only: a rejection produced by framework validation, by middleware, "
            "or by a helper the handler calls is real and does not appear here. Response "
            "fields are the literal keys of dictionaries the handler returns, so a "
            "response assembled elsewhere shows none. Routes registered inside "
            "test files are excluded: they exercise the framework rather than "
            "forming part of the served surface, and they are reported "
            "separately as `test_route` claims."
        ),
    )


def _security_matrix(context: PanelContext) -> Panel:
    """Every security control in one table, with the verdict already reached.

    Nothing here is new: each row restates a section's own determination and
    counts the claims that evidence it. What it adds is that a reviewer asking
    "what protects this system" reads one table instead of nine sections, and
    can see at a glance which rows were checked and found nothing versus which
    were never applicable.
    """

    rows: list[tuple[str, ...]] = []
    for label, section_id, categories in SECURITY_CONTROLS:
        verdict = context.section_verdicts.get(section_id)
        evidence = [
            claim
            for category in categories
            for claim in context.claims_by_category.get(category, ())
        ]
        if verdict is None:
            state = "not probed"
        elif verdict == "absent":
            state = "**absent**"
        elif verdict == "degenerate":
            state = "partial"
        elif verdict == "structural":
            state = "not probed"
        else:
            state = "present"
        located = ""
        for claim in evidence:
            location = context.claim_locations.get(str(claim.get("claim_id", "")))
            if location:
                located = location
                break
        # A control found absent often has claims attached, and they document
        # the absence rather than evidence the control — an authentication
        # census reporting that no route declares one is not a point in
        # authentication's favour. Counting them here would repeat, in a new
        # place, the inversion the sourced-claim probe exists to prevent.
        supported = state not in {"**absent**", "not probed"}
        rows.append(
            (
                label,
                state,
                f"{len(evidence):,}" if supported else "—",
                (located or "—") if supported else "—",
                section_id,
            )
        )
    absent = sum(1 for row in rows if row[1] == "**absent**")
    return Panel(
        name="security_matrix",
        title="Security control matrix",
        columns=("Control", "Determination", "Evidencing claims", "First evidence", "Section"),
        alignments=("left", "left", "right", "left", "left"),
        rows=tuple(rows),
        note=(
            f"{absent:,} of {len(rows):,} controls were probed and found absent. Each "
            "row restates the determination its own section reached, so nothing is "
            "asserted here that is not already evidenced there — a row marked absent "
            "prints its queries in that section. `not probed` means this profile "
            "declares no probe for the concern, which is different from having "
            "checked and found nothing."
        ),
    )


def _multi_role(roles: tuple[MultiRole, ...]) -> Panel:
    """Structures carrying concerns from more than one family.

    The useful sentences in a long specification are rarely single facts; they
    are coincidences. A cache that is also the work queue means a zone never
    loaded is never simulated, and neither claim carries that alone. This is
    the mechanical half of that observation: which structures turn out to do
    two jobs, so a reader knows where a change will not do only what its
    author intended.
    """

    rows = tuple(
        (
            item.structure,
            f"{len(item.families)}",
            ", ".join(item.families),
            ", ".join(f"`{name}`" for name in item.categories),
            item.location,
        )
        for item in roles
    )
    return Panel(
        name="multi_role_structures",
        title="Structures carrying more than one concern",
        columns=("Structure", "Concerns", "Families", "Claim categories", "Declared at"),
        alignments=("left", "right", "left", "left", "left"),
        rows=rows[:MAX_SYMBOL_ROWS],
        note=(
            "Nothing here is asserted that a claim did not already say; what is "
            "added is that two of them are about the same object. Facets of one "
            "concern are excluded — every route also produces an inventory and a "
            "framework-behaviour claim, and counting those would report taxonomy "
            "as coincidence. A census claim attaches to everything it surveyed "
            "and is excluded for the same reason."
        ),
    )


def _substitute_analysis(substitutes: tuple[Substitute, ...]) -> Panel:
    """What plays each absent concern's part, since the work happens regardless.

    Reporting that a repository has no broker is true and incomplete: a list
    appended to and trimmed is a queue with a capacity and a loss mode. An
    absent verdict that stops at the absence sends a reader to grep.
    """

    rows: list[tuple[str, ...]] = []
    for substitute in substitutes:
        for structure in substitute.structures:
            rows.append((substitute.concern, structure.name, structure.role, structure.location))
    caveats = " ".join(item.caveat for item in substitutes)
    return Panel(
        name="substitute_analysis",
        title="What stands in for an absent concern",
        columns=("Absent concern", "Structure", "Why it plays that part", "Declared at"),
        alignments=("left", "left", "left", "left"),
        rows=tuple(rows),
        note=((caveats + " ") if caveats else "")
        + (
            "A substitute is a structural resemblance and not an equivalence. "
            "Nothing here recommends adopting the product it stands in for; "
            "that is an engineering decision this document has no standing to "
            "make."
        ),
    )


def _documented_values(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """What the documentation asserts, set beside what the code declares.

    Documentation is the only artifact in a repository that states intent, and
    the only one that goes stale in silence: nothing fails when a README keeps
    claiming a limit the code stopped using. Every other panel here reports
    what the code does. This one reports what the repository says it does, and
    marks the rows where those disagree.
    """

    declared: dict[str, tuple[str, str]] = {}
    for symbol in symbols:
        metadata = symbol.get("metadata", {})
        for name, entry in (metadata.get("tunables") or {}).items():
            value = entry.get("value")
            rendered = (
                str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
            )
            declared.setdefault(name, (rendered, f"{symbol['path']}:{entry.get('line', 1)}"))
        for name, entry in (metadata.get("string_constants") or {}).items():
            declared.setdefault(name, (str(entry["value"]), f"{symbol['path']}:{entry['line']}"))

    rows: list[tuple[str, ...]] = []
    for symbol in symbols:
        facts = symbol.get("metadata", {}).get("documented_facts") or {}
        for name, entry in sorted(facts.items()):
            values = [str(item) for item in entry.get("values", [])]
            match = declared.get(name) or declared.get(name.rsplit(".", 1)[-1])
            if match is None:
                agreement = "not a declared constant"
                in_code = "—"
            else:
                in_code, _ = match
                if not values:
                    agreement = "no value documented"
                elif in_code in values:
                    agreement = "agrees"
                else:
                    agreement = "**disagrees**"
            rows.append(
                (
                    name,
                    ", ".join(f"`{item}`" for item in values[:4]) or "—",
                    f"`{in_code}`" if in_code != "—" else "—",
                    agreement,
                    f"{symbol['path']}:{entry['line']}",
                )
            )
    # Disagreements first: they are the reason to read the table at all.
    rows.sort(key=lambda row: (row[3] != "**disagrees**", row[4], row[0]))
    disagreements = sum(1 for row in rows if row[3] == "**disagrees**")
    comparable = sum(1 for row in rows if row[3] in {"agrees", "**disagrees**"})
    # Reporting only the disagreement count would read as a clean bill of
    # health on a repository where nothing was comparable in the first place.
    # How many could be checked is the number that qualifies the other one.
    return Panel(
        name="documented_values",
        title="Values the documentation asserts",
        columns=("Name", "Documented", "In code", "Agreement", "Stated at"),
        alignments=("left", "left", "left", "left", "left"),
        rows=tuple(rows[:MAX_SYMBOL_ROWS]),
        note=(
            f"{len(rows):,} names are asserted by documentation. {comparable:,} of them "
            f"also name a constant this code declares and could therefore be checked; "
            f"{disagreements:,} of those disagree. The rest are functions, files and "
            "types rather than values, and are left unjudged rather than called "
            "wrong. A number is attributed to an identifier by proximity within a "
            "line, so this reports what the document says, not what is true."
        ),
    )


def _external_calls(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Calls made through an imported name: the runtime surface actually used.

    An import edge says `os` is a dependency. It does not say whether what is
    called through it is `os.path.join` or `os.system`, which is the same
    import and not the same risk.
    """

    totals: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        calls = symbol.get("metadata", {}).get("external_calls") or {}
        for name, entry in calls.items():
            running = totals.setdefault(
                name,
                {
                    "count": 0,
                    "via": str(entry.get("via", "")),
                    "origin": str(entry.get("origin", "")),
                    "site": f"{symbol['path']}:{entry['first_line']}",
                },
            )
            running["count"] = int(running["count"]) + int(entry["count"])
    # A dependency first, then this repository, then the standard library.
    # Ranked purely by how often each name is called, `random.choice` at
    # forty-four sites outranked the two calls reaching a language model, and
    # the panel answering "what does this program touch" led with `time.time`.
    # Within a rank the count still decides.
    order = {"dependency": 0, "this repository": 1, "standard library": 2}
    rows = tuple(
        (
            name,
            str(entry["via"]),
            str(entry["origin"]) or "—",
            f"{int(entry['count']):,}",
            str(entry["site"]),
        )
        for name, entry in sorted(
            totals.items(),
            key=lambda item: (
                order.get(str(item[1].get("origin", "")), 3),
                -int(item[1]["count"]),
                item[0],
            ),
        )
    )
    return Panel(
        name="external_calls",
        title="Calls through imported names",
        columns=("Call", "Imported as", "Origin", "Sites", "First seen"),
        alignments=("left", "left", "left", "right", "left"),
        rows=rows[:MAX_SYMBOL_ROWS],
        note=(
            "The receiver has to trace back to an import, so a call on a "
            "parameter is this module's own wiring and is excluded. A name "
            "bound from a call to an imported name is followed one step, so a "
            "client constructed from an SDK counts as that SDK — including one "
            "held on an attribute, which is the shape an SDK client usually "
            "takes: `self.client = AsyncOpenAI(...)` followed by "
            "`self.client.chat.completions.create(...)` is reported as an "
            "`openai` call. Rows are ordered by origin before frequency, "
            "because a call leaving the process is what this panel is for: "
            "sorted by count alone, forty-four calls to `random.choice` "
            "outrank two that reach a language model. `dependency` is a "
            "residual — neither the standard library nor a module this "
            "repository defines — and is not a claim that a manifest declares "
            "it."
        ),
    )


def _config_settings(symbols: tuple[dict[str, Any], ...]) -> Panel:
    """Build and compiler settings, which decide what the toolchain accepts."""

    rows: list[tuple[str, ...]] = []
    for symbol in symbols:
        settings = symbol.get("metadata", {}).get("config_settings") or {}
        for key, value in sorted(settings.items()):
            rows.append((key, f"`{value}`", str(symbol["path"])))
    rows.sort(key=lambda row: (row[2], row[0]))
    return Panel(
        name="config_settings",
        title="Compiler and build settings",
        columns=("Setting", "Value", "Declared in"),
        alignments=("left", "left", "left"),
        rows=tuple(rows[:MAX_SYMBOL_ROWS]),
        note=(
            "Scalar settings only, flattened to dotted keys. `strict` set to "
            "false is a fact about how much of a codebase the type checker "
            "actually checks, and it lives in a file no language analyzer opens."
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
    if name == "signatures":
        return _signatures(context.symbols)
    if name == "object_keys":
        return _object_keys(context.symbols)
    if name == "data_flow":
        return _data_flow(context)
    if name == "endpoint_catalog":
        return _endpoint_catalog(context.symbols)
    if name == "security_matrix":
        return _security_matrix(context)
    if name == "multi_role_structures":
        return _multi_role(context.roles)
    if name == "substitute_analysis":
        return _substitute_analysis(context.substitutes)
    if name == "documented_values":
        return _documented_values(context.symbols)
    if name == "external_calls":
        return _external_calls(context.symbols)
    if name == "config_settings":
        return _config_settings(context.symbols)
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
