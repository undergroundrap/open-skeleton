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

_ROUTE_CLAIM = re.compile(
    r"^(?P<method>[A-Z]+) (?P<path>\S+) is handled by (?P<handler>.+)\.$"
)


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
        routes.append(
            (match.group("method"), match.group("path"), match.group("handler"))
        )

    if not routes:
        return _omitted(name, title, "No verified HTTP route claim exists for this snapshot.")

    grouped: dict[str, list[tuple[str, str]]] = {}
    for method, path, handler in sorted(dict.fromkeys(routes)):
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


def build_diagram(
    name: str,
    *,
    files: tuple[dict[str, Any], ...],
    claims: tuple[dict[str, Any], ...],
    symbols: tuple[dict[str, Any], ...],
    edges: tuple[dict[str, Any], ...],
) -> Diagram:
    """Render one named diagram from pinned snapshot records."""

    if name == "module_dependency":
        return _module_dependency(symbols, edges)
    if name == "route_surface":
        return _route_surface(claims)
    if name == "concentration":
        return _concentration(files)
    return _omitted(name, name, f"No generator is registered for diagram '{name}'.")
