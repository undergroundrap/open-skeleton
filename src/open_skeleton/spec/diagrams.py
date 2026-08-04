# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Mermaid projections of the ledger graph.

Every generator draws only from structured records — edges, symbols, and claim
fields the analyzers emit deliberately. Nothing here infers a relationship from
narrative text, so a diagram is either backed by counted edges or omitted with a
stated reason. Truncation is always reported rather than silently applied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_DIAGRAM_NODES = 40
MAX_DIAGRAM_EDGES = 60

_ROUTE_CLAIM = re.compile(r"^(?P<method>[A-Z]+) (?P<path>\S+) is handled by (?P<handler>.+)\.$")


@dataclass(frozen=True, slots=True)
class Diagram:
    """One rendered diagram, or a recorded reason why none could be drawn."""

    name: str
    title: str
    mermaid: str | None
    node_count: int
    edge_count: int
    truncated: bool
    omitted_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "mermaid": self.mermaid,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "truncated": self.truncated,
            "omitted_reason": self.omitted_reason,
        }


def _omitted(name: str, title: str, reason: str) -> Diagram:
    return Diagram(
        name=name,
        title=title,
        mermaid=None,
        node_count=0,
        edge_count=0,
        truncated=False,
        omitted_reason=reason,
    )


def _node_id(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", value).strip("_")
    return cleaned or "node"


def _module_dependency(
    symbols: tuple[dict[str, Any], ...], edges: tuple[dict[str, Any], ...]
) -> Diagram:
    name, title = "module_dependency", "Internal module dependencies"
    modules = {
        str(item["qualified_name"]): str(item["path"])
        for item in symbols
        if str(item["kind"]) == "module"
    }
    if not modules:
        return _omitted(name, title, "No module symbols were recorded for this snapshot.")

    path_by_module = modules
    module_by_path = {path: module for module, path in path_by_module.items()}

    resolved: list[tuple[str, str]] = []
    for edge in edges:
        if str(edge["relationship"]) != "imports":
            continue
        source = module_by_path.get(str(edge["source_path"]))
        if source is None:
            continue
        target_ref = str(edge["target_ref"]).lstrip(".")
        target = next(
            (
                module
                for module in path_by_module
                if module == target_ref or module.endswith(f".{target_ref}")
            ),
            None,
        )
        if target is None or target == source:
            continue
        resolved.append((source, target))

    unique = sorted(dict.fromkeys(resolved))
    if not unique:
        return _omitted(
            name,
            title,
            "No import edge resolved to another module inside this repository.",
        )

    truncated = len(unique) > MAX_DIAGRAM_EDGES
    shown = unique[:MAX_DIAGRAM_EDGES]
    nodes = sorted({item for pair in shown for item in pair})

    lines = ["flowchart LR"]
    for module in nodes:
        lines.append(f'    {_node_id(module)}["{module}"]')
    for source, target in shown:
        lines.append(f"    {_node_id(source)} --> {_node_id(target)}")

    return Diagram(
        name=name,
        title=title,
        mermaid="\n".join(lines),
        node_count=len(nodes),
        edge_count=len(shown),
        truncated=truncated,
    )


def _route_surface(claims: tuple[dict[str, Any], ...]) -> Diagram:
    name, title = "route_surface", "HTTP route surface by prefix"
    routes: list[tuple[str, str, str]] = []
    for claim in claims:
        if str(claim["category"]) != "http_route":
            continue
        match = _ROUTE_CLAIM.match(str(claim["claim"]))
        if match is None:
            continue
        routes.append((match.group("method"), match.group("path"), match.group("handler")))

    if not routes:
        return _omitted(name, title, "No verified HTTP route claim exists for this snapshot.")

    grouped: dict[str, list[tuple[str, str]]] = {}
    for method, path, _handler in sorted(dict.fromkeys(routes)):
        segments = [part for part in path.split("/") if part and not part.startswith("{")]
        prefix = f"/{segments[0]}" if segments else "/"
        grouped.setdefault(prefix, []).append((method, path))

    truncated = False
    lines = ["flowchart LR", '    CLIENT["HTTP client"]']
    edge_count = 0
    for prefix in sorted(grouped):
        entries = grouped[prefix]
        group_id = f"g_{_node_id(prefix)}"
        lines.append(f'    {group_id}["{prefix}<br/>{len(entries)} routes"]')
        lines.append(f"    CLIENT --> {group_id}")
        edge_count += 1
        if len(lines) > MAX_DIAGRAM_NODES * 2:
            truncated = True
            break

    return Diagram(
        name=name,
        title=title,
        mermaid="\n".join(lines),
        node_count=len(grouped) + 1,
        edge_count=edge_count,
        truncated=truncated,
    )


def _concentration(files: tuple[dict[str, Any], ...]) -> Diagram:
    name, title = "concentration", "Line-count concentration by file"
    ranked = sorted(files, key=lambda item: -int(item["line_count"]))[:10]
    if not ranked or int(ranked[0]["line_count"]) == 0:
        return _omitted(name, title, "No file in this snapshot recorded a positive line count.")

    total = sum(int(item["line_count"]) for item in files) or 1
    lines = ["flowchart TD", '    REPO["Repository"]']
    for item in ranked:
        share = int(item["line_count"]) / total
        label = f"{item['path']}<br/>{int(item['line_count']):,} lines ({share:.1%})"
        lines.append(f'    REPO --> {_node_id(str(item["path"]))}["{label}"]')

    return Diagram(
        name=name,
        title=title,
        mermaid="\n".join(lines),
        node_count=len(ranked) + 1,
        edge_count=len(ranked),
        truncated=len(files) > len(ranked),
    )


def _receivers_by_file(
    symbols: tuple[dict[str, Any], ...], edges: tuple[dict[str, Any], ...]
) -> dict[str, frozenset[str]]:
    """Per file, the names that identify a module-level collaborator.

    A call target such as ``result.get`` names a local value and tells a reader
    nothing about system structure; ``vec_db.save_player`` names a module-owned
    object and does. The distinction has to be made per file — resolving against
    every symbol in the repository lets a local named ``player`` collide with an
    unrelated module-level ``player`` elsewhere and pollute the diagram.
    """

    receivers: dict[str, set[str]] = {}
    for symbol in symbols:
        if str(symbol["kind"]) not in {"module_variable", "class"}:
            continue
        receivers.setdefault(str(symbol["path"]), set()).add(
            str(symbol["qualified_name"]).rsplit(".", 1)[-1]
        )
    for edge in edges:
        if str(edge["relationship"]) != "imports":
            continue
        target = str(edge["target_ref"]).lstrip(".")
        if target:
            receivers.setdefault(str(edge["source_path"]), set()).add(target.rsplit(".", 1)[-1])
    return {path: frozenset(names) for path, names in receivers.items()}


def _route_sequences(
    claims: tuple[dict[str, Any], ...],
    symbols: tuple[dict[str, Any], ...],
    edges: tuple[dict[str, Any], ...],
    evidence_by_id: dict[str, dict[str, Any]],
    limit: int,
) -> tuple[Diagram, ...]:
    name, title = "route_sequence", "Route interaction sequence"
    symbol_by_name = {str(item["qualified_name"]): item for item in symbols}
    receivers_by_file = _receivers_by_file(symbols, edges)

    calls_by_source: dict[str, list[tuple[int, str]]] = {}
    for edge in edges:
        if str(edge["relationship"]) != "calls":
            continue
        source_id = edge.get("source_symbol_id")
        if source_id is None:
            continue
        record = evidence_by_id.get(str(edge.get("evidence_id") or ""))
        line = int(record["start_line"]) if record and record["start_line"] else 0
        calls_by_source.setdefault(str(source_id), []).append((line, str(edge["target_ref"])))

    candidates: list[tuple[str, str, list[tuple[str, str]]]] = []
    for claim in claims:
        if str(claim["category"]) != "http_route":
            continue
        match = _ROUTE_CLAIM.match(str(claim["claim"]))
        if match is None:
            continue
        handler = symbol_by_name.get(match.group("handler"))
        if handler is None:
            continue
        # Decorator calls sit above the `def` line; they register the route
        # rather than participate in handling it.
        body_start = int(handler["start_line"])
        receivers = receivers_by_file.get(str(handler["path"]), frozenset())
        steps: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for line, target in sorted(calls_by_source.get(str(handler["symbol_id"]), [])):
            if line < body_start or "." not in target:
                continue
            receiver, _, method = target.rpartition(".")
            receiver = receiver.rsplit(".", 1)[-1]
            if receiver not in receivers:
                continue
            step = (receiver, method)
            if step in seen:
                continue
            seen.add(step)
            steps.append(step)
        if steps:
            candidates.append(
                (f"{match.group('method')} {match.group('path')}", match.group("handler"), steps)
            )

    if not candidates:
        return (
            _omitted(
                name,
                title,
                "No route handler recorded a call that resolves to a module-level "
                "collaborator, so no interaction could be drawn without guessing.",
            ),
        )

    candidates.sort(key=lambda item: (-len(item[2]), item[0]))
    diagrams: list[Diagram] = []
    for route, handler_name, steps in candidates[:limit]:
        shown = steps[:MAX_DIAGRAM_EDGES]
        participants = sorted({receiver for receiver, _ in shown})
        lines = ["sequenceDiagram", "    participant Client"]
        short_handler = handler_name.rsplit(".", 1)[-1]
        handler_id = _node_id(short_handler)
        lines.append(f"    participant {handler_id} as {short_handler}")
        for participant in participants:
            lines.append(f"    participant {_node_id(participant)} as {participant}")
        lines.append(f"    Client->>{handler_id}: {route}")
        for receiver, method in shown:
            lines.append(f"    {handler_id}->>{_node_id(receiver)}: {method}()")
        diagrams.append(
            Diagram(
                name=f"{name}:{route}",
                title=f"{title} — {route}",
                mermaid="\n".join(lines),
                node_count=len(participants) + 2,
                edge_count=len(shown) + 1,
                truncated=len(steps) > len(shown),
            )
        )
    return tuple(diagrams)


def _persistence_erd(claims: tuple[dict[str, Any], ...]) -> Diagram:
    name, title = "persistence_erd", "Durable storage entities and access"
    creators = re.compile(r"^(?P<symbol>\S+) creates \S+ table (?P<table>\w+)\.$")
    writers = re.compile(r"^(?P<symbol>\S+) serializes \S+ into \S+ table (?P<table>\w+)\.$")

    tables: dict[str, dict[str, set[str]]] = {}
    for claim in claims:
        text = str(claim["claim"])
        for pattern, role in ((creators, "creates"), (writers, "writes")):
            match = pattern.match(text)
            if match is None:
                continue
            entry = tables.setdefault(match.group("table"), {"creates": set(), "writes": set()})
            entry[role].add(match.group("symbol").rsplit(".", 1)[-1])

    if not tables:
        return _omitted(name, title, "No claim records a durable table for this snapshot.")

    lines = ["flowchart LR"]
    accessors: set[str] = set()
    edge_count = 0
    for table in sorted(tables):
        table_id = f"t_{_node_id(table)}"
        lines.append(f'    {table_id}[("{table}")]')
    for table in sorted(tables):
        for role in ("creates", "writes"):
            for accessor in sorted(tables[table][role]):
                accessor_id = f"a_{_node_id(accessor)}"
                if accessor not in accessors:
                    accessors.add(accessor)
                    lines.append(f'    {accessor_id}["{accessor}"]')
                lines.append(f"    {accessor_id} -->|{role}| t_{_node_id(table)}")
                edge_count += 1

    return Diagram(
        name=name,
        title=title,
        mermaid="\n".join(lines),
        node_count=len(tables) + len(accessors),
        edge_count=edge_count,
        truncated=False,
    )


def build_diagrams(
    name: str,
    *,
    files: tuple[dict[str, Any], ...],
    claims: tuple[dict[str, Any], ...],
    symbols: tuple[dict[str, Any], ...],
    edges: tuple[dict[str, Any], ...],
    evidence_by_id: dict[str, dict[str, Any]],
    route_sequence_limit: int = 6,
) -> tuple[Diagram, ...]:
    """Render every diagram a named generator produces for this snapshot."""

    if name == "module_dependency":
        return (_module_dependency(symbols, edges),)
    if name == "route_surface":
        return (_route_surface(claims),)
    if name == "concentration":
        return (_concentration(files),)
    if name == "persistence_erd":
        return (_persistence_erd(claims),)
    if name == "route_sequence":
        return _route_sequences(claims, symbols, edges, evidence_by_id, route_sequence_limit)
    return (_omitted(name, name, f"No generator is registered for diagram '{name}'."),)
