# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import ast
import hashlib
import re
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from open_skeleton.ids import stable_id
from open_skeleton.models import (
    AnalysisResult,
    ClaimRecord,
    CoverageRecord,
    EdgeRecord,
    EvidenceRecord,
    FileRecord,
    Snapshot,
    SymbolRecord,
    utc_now,
)
from open_skeleton.policy import describes_the_product

ANALYZER_NAME = "python-ast"
ANALYZER_VERSION = "python-ast/v3"
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "options", "head"})
STDLIB_HTTP_HANDLER_BASES = frozenset({"BaseHTTPRequestHandler", "SimpleHTTPRequestHandler"})
MUTATING_METHODS = frozenset(
    {
        "add",
        "append",
        "clear",
        "discard",
        "extend",
        "insert",
        "pop",
        "popitem",
        "remove",
        "reverse",
        "setdefault",
        "sort",
        "update",
    }
)
CREATE_TABLE_PATTERN = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`\[]?([A-Za-z_][\w]*)",
    re.IGNORECASE,
)
ROUTE_PATH_LITERAL = re.compile(r"^/[A-Za-z0-9_\-./{}]*$")
ENDPOINT_LITERAL = re.compile(r"^(?:https?|ws|wss)://[^\s'\"]+$", re.IGNORECASE)
# String methods whose argument is something being looked for, not dialed.
STRING_MATCH_METHODS = frozenset(
    {
        "count",
        "find",
        "rfind",
        "index",
        "startswith",
        "endswith",
        "split",
        "rsplit",
        "partition",
        "replace",
        "strip",
        "lstrip",
        "rstrip",
        "match",
        "search",
        "compile",
    }
)
# Mapping keys whose value names a vocabulary rather than a host to contact.
IDENTIFIER_KEYS = frozenset({"$schema", "$id", "xmlns", "namespace", "schema"})
# Warning classes that announce a scheduled removal rather than a defect.
DEPRECATION_CATEGORIES = frozenset(
    {"DeprecationWarning", "PendingDeprecationWarning", "FutureWarning"}
)
# Categories that describe the system when the evidence is source and describe
# the suite when it is a test. Nothing is dropped: a fixture's shape is a real
# fact about the suite, and a reader deciding what the system stores needs to
# know which of the two they are looking at.
TEST_SCOPED_CATEGORIES = {
    "storage": "test_storage",
    "storage_schema": "test_storage_schema",
    "configuration_read": "test_configuration_read",
    "schema_migration": "test_schema_migration",
    "http_route": "test_route",
    "external_call": "test_external_call",
    # What a suite absorbs is not the program's error contract. This
    # repository reported "1 handler(s) catch `OSError, ValueError`"
    # from a test's own `except` around a file it was deliberately
    # failing to write, and the audit flagged it as production error
    # handling evidenced only by tests. The audit was right; the claim
    # should never have carried that category in the first place.
    "caught_exception": "test_caught_exception",
    "exception_type": "test_exception_type",
    "collection_driven_workset": "test_collection_driven_workset",
}
INSERT_TABLE_PATTERN = re.compile(
    r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+[\"`\[]?([A-Za-z_][\w]*)",
    re.IGNORECASE,
)
# A migration is routinely built with an f-string, so the table and column may
# be interpolated. `{}` stands for a value the analyzer cannot resolve, and the
# claim says so rather than inventing a name.
ALTER_TABLE_PATTERN = re.compile(
    r"\bALTER\s+TABLE\s+(?P<table>\{\}|[\"`\[]?[A-Za-z_]\w*[\"`\]]?)\s+ADD\s+"
    r"(?:COLUMN\s+)?(?P<column>\{\}|[\"`\[]?[A-Za-z_]\w*[\"`\]]?)",
    re.IGNORECASE,
)


def _package_directories(paths: Iterable[str]) -> frozenset[str]:
    """Directories carrying an ``__init__.py``, which is what makes them packages.

    Derived from the file list rather than assumed from layout conventions, so
    a project that puts its package somewhere unusual is read correctly and a
    directory merely named ``src`` is not given meaning it has not earned.
    """

    return frozenset(
        path.rsplit("/", 1)[0] if "/" in path else ""
        for path in paths
        if path.rsplit("/", 1)[-1] == "__init__.py"
    )


def _module_parts(path: str, packages: frozenset[str]) -> tuple[str, str]:
    """The import root and the importable module name for a Python file.

    A module's name is only meaningful relative to the directory that would sit
    on ``sys.path``. Joining the whole path instead produced
    ``src.open_skeleton.ledger`` -- a name nothing can import, and one that
    reads plausibly enough to survive five repository shapes unnoticed. It took
    a workspace of nine projects, where the same defect rendered as
    ``open-skeleton.src.open_skeleton.ledger``, to make it visible.

    The name therefore begins at the first ancestor carrying an
    ``__init__.py``. When no ancestor has one the path is left exactly as it
    was, because absence of that file is not evidence of a layout: PEP 420
    namespace packages are importable without it, and assuming otherwise
    renamed ``app.core.used`` to ``used`` and lost the import that referenced
    it. Rewriting only on positive evidence is the whole rule.
    """

    parts = path.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return "", "__root__"
    for index in range(len(parts)):
        if "/".join(parts[: index + 1]) in packages:
            return "/".join(parts[:index]), ".".join(parts[index:])
    return "", ".".join(parts)


def _module_name(path: str, packages: frozenset[str] = frozenset()) -> str:
    return _module_parts(path, packages)[1]


def _module_names(paths: Iterable[str], packages: frozenset[str]) -> dict[str, str]:
    """Importable name per path, qualified further only where two files collide.

    Two distributions in one workspace can each own a ``server.py``, and both
    are importable as ``server``. Letting them share a qualified name would
    merge two unrelated files into one identity, so a colliding name is
    prefixed with its import root -- which is the only thing that actually
    distinguishes them. Names that do not collide are left alone, because the
    prefix carries no information there.
    """

    claimed: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path in paths:
        root, name = _module_parts(path, packages)
        claimed[name].append((root, path))
    resolved: dict[str, str] = {}
    for name, owners in claimed.items():
        for root, path in owners:
            if len(owners) == 1 or not root:
                resolved[path] = name
            else:
                resolved[path] = f"{root.replace('-', '_').replace('/', '.')}.{name}"
    return resolved


def _expr_name(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _expr_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    if isinstance(node, ast.Call):
        return _expr_name(node.func)
    if isinstance(node, ast.Subscript):
        return _expr_name(node.value)
    return None


def _iterated_collection(node: ast.AST) -> str | None:
    """Name the collection that supplies a loop, through harmless wrappers.

    ``for key in list(store._pending.keys())`` is structurally a loop over
    ``store._pending``. Recording ``list`` or ``keys`` as the source loses the
    architectural fact: membership in the collection decides which work can
    run. Only wrappers that preserve the collection's members are removed.
    """

    current = node
    for _ in range(4):
        if not isinstance(current, ast.Call):
            break
        called = _expr_name(current.func) or ""
        final = called.rsplit(".", maxsplit=1)[-1]
        if final in {"list", "tuple", "set", "iter", "sorted", "reversed"}:
            if len(current.args) != 1 or current.keywords:
                return None
            current = current.args[0]
            continue
        if final in {"keys", "values", "items"} and isinstance(current.func, ast.Attribute):
            current = current.func.value
            continue
        break
    return _expr_name(current)


class _LoopWorksetCollector(ast.NodeVisitor):
    """Resolve one local alias between an imported collection and a loop."""

    def __init__(self) -> None:
        self.aliases: list[dict[str, str]] = [{}]
        self.worksets: dict[int, str] = {}

    def _resolved(self, node: ast.AST) -> str | None:
        value = _iterated_collection(node)
        if value is None:
            return None
        return self.aliases[-1].get(value, value)

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.aliases.append({})
        for statement in node.body:
            self.visit(statement)
        self.aliases.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        source = self._resolved(node.value)
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if source is None:
                self.aliases[-1].pop(target.id, None)
            else:
                self.aliases[-1][target.id] = source
        self.generic_visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            source = self._resolved(node.value)
            if source is None:
                self.aliases[-1].pop(node.target.id, None)
            else:
                self.aliases[-1][node.target.id] = source
            self.generic_visit(node.value)

    def _visit_loop(self, node: ast.For | ast.AsyncFor) -> None:
        source = self._resolved(node.iter)
        if source is not None:
            self.worksets[id(node)] = source
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_loop(node)


def _loop_worksets(tree: ast.Module) -> dict[int, str]:
    collector = _LoopWorksetCollector()
    collector.visit(tree)
    return collector.worksets


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _sql_shape(node: ast.AST | None) -> str | None:
    """SQL text with interpolated values replaced by a placeholder.

    Schema statements are commonly assembled with an f-string, which makes them
    invisible to a literal-only reader. The static skeleton is still enough to
    tell what kind of statement it is, so it is recovered with `{}` marking
    each value the analyzer cannot resolve.
    """

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts)
    return None


def _literal_number(node: ast.AST | None) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _literal_number(node.operand)
        return -value if value is not None else None
    return None


def _assigned_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for item in node.elts for name in _assigned_names(item)]
    return []


def _is_mutable_initializer(node: ast.AST | None) -> bool:
    if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(node, ast.Call):
        name = (_expr_name(node.func) or "").split(".")[-1]
        return name in {"dict", "list", "set", "defaultdict", "deque", "WeakKeyDictionary"}
    return False


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left = _expr_name(test.left)
    right = _literal_string(test.comparators[0])
    return (left, right) == ("__name__", "__main__") or (
        _literal_string(test.left),
        _expr_name(test.comparators[0]),
    ) == ("__main__", "__name__")


MAX_FLOW_NODES = 24
MAX_FLOW_LABEL = 68


def _flow_label(node: ast.AST) -> str:
    """Render an expression back to source, bounded, for a diagram label."""

    try:
        text = ast.unparse(node)
    except (AttributeError, ValueError):  # pragma: no cover - unparse is stdlib
        return "<expression>"
    text = " ".join(text.split())
    if len(text) > MAX_FLOW_LABEL:
        text = text[: MAX_FLOW_LABEL - 1] + "…"
    return text


def _raise_summary(node: ast.Raise) -> str | None:
    """Describe a raise, preferring an HTTP status when one is literal."""

    exception = node.exc
    if exception is None:
        return "re-raise"
    name = (_expr_name(exception) or "").split(".")[-1]
    if isinstance(exception, ast.Call) and name == "HTTPException":
        for keyword in exception.keywords:
            if keyword.arg == "status_code":
                value = _literal_number(keyword.value)
                if value is not None:
                    return f"HTTP {int(value)}"
        if exception.args:
            value = _literal_number(exception.args[0])
            if value is not None:
                return f"HTTP {int(value)}"
        return "HTTPException"
    return name or "raise"


def _raise_message(node: ast.Raise) -> str | None:
    """The literal text a raise gives the caller, when it gives one.

    "HTTP 404" says a request was refused. "Player not found" says why, and
    it is the string an operator greps for when the log line arrives. The
    status code was recorded and the message thrown away.

    Only a literal is read. An f-string or a variable has no fixed text, and a
    message quoted wrongly is worse than a message omitted, because a reader
    will search for the words this document gave them.
    """

    exception = node.exc
    if not isinstance(exception, ast.Call):
        return None
    for keyword in exception.keywords:
        if (
            keyword.arg == "detail"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value.strip() or None
    if exception.args:
        first = exception.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value.strip() or None
    return None


def _control_flow(node: ast.AST) -> list[dict[str, Any]]:
    """Ordered guards, raises, and returns directly inside one function body.

    This is a guard-and-exit trace, not a control-flow graph: it records the
    decisions and terminations a reader follows to understand when a handler
    rejects a request. Nested function and class definitions are not entered,
    because their bodies run under a different call, and loops are recorded as
    a single node rather than unrolled.
    """

    events: list[dict[str, Any]] = []

    def walk(statements: list[ast.stmt], depth: int) -> None:
        if depth > 3:
            return
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(statement, ast.If):
                events.append(
                    {
                        "kind": "guard",
                        "line": statement.lineno,
                        "label": _flow_label(statement.test),
                        "depth": depth,
                    }
                )
                walk(statement.body, depth + 1)
                walk(statement.orelse, depth + 1)
                continue
            if isinstance(statement, ast.Raise):
                summary = _raise_summary(statement)
                if summary:
                    event: dict[str, Any] = {
                        "kind": "raise",
                        "line": statement.lineno,
                        "label": summary,
                        "depth": depth,
                    }
                    message = _raise_message(statement)
                    if message:
                        event["message"] = message
                    events.append(event)
                continue
            if isinstance(statement, ast.Return):
                events.append(
                    {
                        "kind": "return",
                        "line": statement.lineno,
                        "label": (_flow_label(statement.value) if statement.value else "None"),
                        "depth": depth,
                    }
                )
                continue
            for field in ("body", "orelse", "finalbody"):
                nested = getattr(statement, field, None)
                if isinstance(nested, list):
                    walk([item for item in nested if isinstance(item, ast.stmt)], depth + 1)
            for handler in getattr(statement, "handlers", []):
                walk(handler.body, depth + 1)

    body = getattr(node, "body", [])
    walk([item for item in body if isinstance(item, ast.stmt)], 0)
    events.sort(key=lambda item: int(item["line"]))
    return events[:MAX_FLOW_NODES]


def _route_subject(node: ast.AST) -> bool:
    """Whether an expression names the request path inside an HTTP handler.

    ``BaseHTTPRequestHandler`` exposes ``self.path`` directly. Code commonly
    parses it once and then compares ``parsed.path`` or a local ``path``. This
    deliberately accepts only those three shapes and only inside a proven
    standard-library handler class; a filesystem variable named ``path`` in an
    ordinary function must not become an endpoint.
    """

    name = _expr_name(node) or ""
    return name == "path" or name.endswith(".path")


def _route_literals(node: ast.AST) -> tuple[tuple[str, int, int], ...]:
    """Literal paths selected by one standard-library request method.

    Framework decorators state routes directly. ``http.server`` instead names
    the verb in ``do_GET``/``do_POST`` and dispatches with ordinary comparisons.
    The generic control-flow projection already sees those comparisons, but the
    route census did not. This extracts only exact equality/membership checks
    and path-prefix checks, preserving the source line that proves each route.
    """

    found: dict[str, tuple[int, int]] = {}

    def record(value: str | None, owner: ast.AST, *, prefix: bool = False) -> None:
        if value is None or not ROUTE_PATH_LITERAL.fullmatch(value):
            return
        path = f"{value}{{remainder}}" if prefix else value
        found.setdefault(
            path,
            (
                int(getattr(owner, "lineno", 1)),
                int(getattr(owner, "end_lineno", getattr(owner, "lineno", 1))),
            ),
        )

    class Collector(ast.NodeVisitor):
        def visit_FunctionDef(self, _child: ast.FunctionDef) -> None:
            # Nested callables have their own request contract, if any.
            return

        def visit_AsyncFunctionDef(self, _child: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, _child: ast.ClassDef) -> None:
            return

        def visit_Compare(self, comparison: ast.Compare) -> None:
            operands = [comparison.left, *comparison.comparators]
            for left, operator, right in zip(
                operands[:-1], comparison.ops, operands[1:], strict=True
            ):
                if isinstance(operator, (ast.Eq, ast.Is)):
                    if _route_subject(left):
                        record(_literal_string(right), comparison)
                    elif _route_subject(right):
                        record(_literal_string(left), comparison)
                elif (
                    isinstance(operator, (ast.In, ast.NotIn))
                    and _route_subject(left)
                    and isinstance(right, (ast.List, ast.Tuple, ast.Set))
                ):
                    for item in right.elts:
                        record(_literal_string(item), comparison)
            self.generic_visit(comparison)

        def visit_Call(self, call: ast.Call) -> None:
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "startswith"
                and _route_subject(call.func.value)
                and call.args
            ):
                record(_literal_string(call.args[0]), call, prefix=True)
            self.generic_visit(call)

    collector = Collector()
    for statement in getattr(node, "body", []):
        collector.visit(statement)
    return tuple((path, start, end) for path, (start, end) in found.items())


MIN_STATE_VALUES = 2
MAX_STATE_FIELDS = 6


def _attribute_field(node: ast.AST) -> str | None:
    """Name the field a value is stored into, e.g. `player.status`."""

    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        key = _literal_string(node.slice)
        return key if key else None
    return None


def _state_fields(tree: ast.Module) -> dict[str, dict[str, Any]]:
    """Observed string values assigned to a field, and the guards preceding them.

    This records what the source does, not what a state machine is presumed to
    be. A field is reported only when at least two distinct string literals are
    assigned to it somewhere in the module. A transition is recorded only when a
    comparison of that same field against a literal appears earlier in the same
    function than an assignment of another literal — an observed ordering, not a
    reachability proof.
    """

    fields: dict[str, dict[str, Any]] = {}

    def record(field: str) -> dict[str, Any]:
        return fields.setdefault(field, {"values": set(), "entries": set()})

    def guarded_assignments(statements: list[ast.stmt], condition: str | None) -> None:
        """Walk statements carrying the nearest enclosing `if` test.

        `elif` parses as a nested `If` inside `orelse`, so each branch must be
        entered as a branch. Labelling it with the outer negation would report a
        condition the source never writes.
        """

        for statement in statements:
            if isinstance(statement, ast.If):
                text = _flow_label(statement.test)
                guarded_assignments(statement.body, text)
                guarded_assignments(statement.orelse, f"not ({text})")
                continue
            _collect(statement, condition)
            for field in ("body", "orelse", "finalbody"):
                nested = getattr(statement, field, None)
                if isinstance(nested, list):
                    guarded_assignments(
                        [item for item in nested if isinstance(item, ast.stmt)], condition
                    )
            for handler in getattr(statement, "handlers", []):
                guarded_assignments(handler.body, condition)

    def _collect(statement: ast.AST, condition: str | None) -> None:
        if not isinstance(statement, ast.Assign):
            return
        literal = _literal_string(statement.value)
        if literal is None:
            return
        for target in statement.targets:
            field = _attribute_field(target)
            if not field:
                continue
            entry = record(field)
            entry["values"].add(literal)
            entry["entries"].add((literal, condition or "", statement.lineno))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            body = [item for item in node.body if isinstance(item, ast.stmt)]
            guarded_assignments(body, None)
        if isinstance(node, ast.Compare):
            field = _attribute_field(node.left)
            if field:
                for comparator in node.comparators:
                    literal = _literal_string(comparator)
                    if literal:
                        record(field)["values"].add(literal)

    return {
        field: {
            "values": sorted(entry["values"]),
            "entries": sorted(entry["entries"]),
        }
        for field, entry in fields.items()
        if len(entry["values"]) >= MIN_STATE_VALUES
    }


def _instance_tunables(tree: ast.Module) -> dict[str, dict[str, Any]]:
    """Numeric literals assigned to `self` attributes anywhere in the module.

    A limit written as `self._cache_limit = 200` inside `__init__` is exactly as
    tunable as one written at module level, and a module-scope walk never sees
    it. The owning class is recorded so two classes using the same attribute
    name stay distinguishable.
    """

    found: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Assign):
                continue
            value = _literal_number(inner.value)
            if value is None:
                continue
            for target in inner.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    found.setdefault(
                        f"{node.name}.{target.attr}",
                        {"value": value, "line": inner.lineno},
                    )
    return found


def _data_containers(tree: ast.Module) -> dict[str, dict[str, Any]]:
    """Module-level list, dict, and set literals, with their element counts.

    These are the lookup tables a system's behaviour is written into. Their size
    is the fact a reader needs first: a 40-entry table is content, a 2-entry one
    is a switch.
    """

    found: dict[str, dict[str, Any]] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        value = statement.value
        if isinstance(value, (ast.List, ast.Set)):
            size, kind = len(value.elts), type(value).__name__.lower()
        elif isinstance(value, ast.Dict):
            size, kind = len(value.keys), "dict"
        else:
            continue
        if size < 2:
            continue
        for name in (n for target in statement.targets for n in _assigned_names(target)):
            found.setdefault(name, {"size": size, "kind": kind, "line": statement.lineno})
    return found


def _dotted_name(node: ast.Attribute) -> str | None:
    """`mob.loot_table` for an attribute chain rooted in a plain name.

    Only chains whose base is a `Name` are spelled out. `get_player().hp` and
    `items[0].name` have no stable written form -- the receiver is an
    expression, not something a reader can search for -- so they are left to
    the bare-attribute entry rather than invented.
    """

    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _name_index(tree: ast.Module) -> dict[str, int]:
    """Every name this module binds or reaches for, with the line it first appears.

    This is a concordance, not analysis. The structured panels report what a
    name *is* — a model field, a signature, a tunable — and each of those is a
    judgement about a name in a particular position. This answers the flatter
    question an agent asks when navigating: does this identifier occur in this
    file, and where does it start.

    It is deliberately exhaustive and deliberately unranked. A local loop
    variable sits beside a public function here, which is why it lives in the
    JSON projection and not in the readable index: presenting the two as equals
    to a human would bury the surface that matters under the noise that does
    not.
    """

    found: dict[str, int] = {}

    def record(name: str, line: int) -> None:
        if name and not name.startswith("__"):
            found[name] = min(found.get(name, line), line)

    for node in ast.walk(tree):
        line = getattr(node, "lineno", 1)
        if isinstance(node, ast.Name):
            record(node.id, line)
        elif isinstance(node, ast.arg):
            record(node.arg, line)
        elif isinstance(node, ast.Attribute):
            record(node.attr, line)
            # `loot_table` does not say what carries it, and `mob.loot_table`
            # is the form somebody searching for it actually types. Recording
            # only the final component discarded the half that identifies the
            # owner, which is most of the value for a reader navigating an
            # unfamiliar domain model.
            dotted = _dotted_name(node)
            if dotted:
                record(dotted, line)
        elif isinstance(node, ast.keyword) and node.arg:
            record(node.arg, line)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            record(node.name, line)
        elif isinstance(node, ast.alias):
            record(node.asname or node.name.split(".")[0], line)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            record(node.name, line)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A dictionary key is a field name in every codebase that returns
            # dictionaries, and it is the name a caller has to spell.
            text = node.value
            if text.isidentifier() and len(text) > 2:
                record(text, line)
    return found


def _call_origin(via: str, local_modules: frozenset[str]) -> str:
    """Where the called name comes from: the standard library, here, or neither.

    A reader asking what a program touches at runtime is asking about the last
    of those. Ranked purely by how often each name is called, `random.choice`
    at forty-four sites outranks the two calls that reach a language model,
    and the panel answering "what does this depend on" leads with `time.time`.

    `sys.stdlib_module_names` is exact and costs nothing. A module the
    repository itself defines is local. What remains is a dependency, which is
    the honest residual: this says the name resolves to neither the standard
    library nor this repository, not that a manifest declares it.
    """

    root = via.split(".", maxsplit=1)[0]
    if root in sys.stdlib_module_names:
        return "standard library"
    # `from app.core import vec_db` binds the bare name while the repository
    # knows the module as `backend.app.core.vec_db`, so a leading-segment test
    # called every local helper a dependency -- on one repository that put
    # `vec_db.get_player`, its own storage layer, at the top of a panel headed
    # by what the program depends on.
    # Suffix match on the whole dotted path. An import is written relative to
    # a source root -- `app.core.vector_db` -- while the repository knows the
    # file as `backend.app.core.vector_db`, so comparing leading segments
    # matches nothing and every local helper reads as a dependency.
    if any(name == via or name.endswith(f".{via}") for name in local_modules):
        return "this repository"
    return "dependency"


def _external_calls(
    tree: ast.Module, local_modules: frozenset[str] = frozenset()
) -> dict[str, dict[str, Any]]:
    """Calls made through an imported name: the runtime surface this module uses.

    An import edge records that `asyncio` is a dependency. It does not record
    that what is called through it is `create_task`, and the difference is what
    a reviewer means by "what does this actually do at runtime" —
    `os.system` and `os.path.join` are the same import and not the same risk.

    The receiver has to trace back to an import, so `self.method()` and a call
    on a parameter are excluded: those are this module's own wiring, not a
    surface it depends on. Names bound from a call to an imported name are
    followed one step, so a client constructed from an SDK counts as that SDK.
    """

    imported: dict[str, str] = {}
    # Where each bound name came from. `from app.core.vector_db import vec_db`
    # binds an *object*, so `via` records `vec_db` and the module it lives in
    # is lost -- which made a repository's own storage layer indistinguishable
    # from a third-party package. This keeps the module without changing what
    # `via` has always meant.
    sources: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                imported[bound] = alias.name
                sources[bound] = node.module or ""
        elif isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b.c` binds `a` unless aliased.
                bound = alias.asname or alias.name.split(".")[0]
                imported[bound] = alias.name
                sources[bound] = alias.name

    # `client = AsyncOpenAI(...)` makes `client` stand for the imported name.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        root = (_expr_name(node.value.func) or "").split(".")[0]
        if root and root in imported:
            for target in node.targets:
                for name in _assigned_names(target):
                    imported.setdefault(name, imported[root])
                    sources.setdefault(name, sources.get(root, ""))
                # `self.client = AsyncOpenAI(...)` is the shape an SDK client
                # actually takes, and binding only bare names missed it: the
                # call that follows is rooted at `self`, which is nothing, so
                # the one operation the service contract consists of --
                # `chat.completions.create` -- was parsed and dropped.
                attribute = _expr_name(target)
                if attribute and "." in attribute:
                    imported.setdefault(attribute, imported[root])
                    sources.setdefault(attribute, sources.get(root, ""))

    found: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        dotted = _expr_name(node.func)
        if not dotted or "." not in dotted:
            continue
        # Longest prefix wins. `self.client.chat.completions.create` is bound
        # at `self.client`; taking the first segment asks whether `self` is an
        # import, which it never is.
        segments = dotted.split(".")
        receiver = next(
            (
                candidate
                for length in range(len(segments) - 1, 0, -1)
                if (candidate := ".".join(segments[:length])) in imported
            ),
            None,
        )
        if receiver is None:
            continue
        via = imported[receiver]
        entry = found.setdefault(
            dotted,
            {
                "count": 0,
                "first_line": node.lineno,
                "via": via,
                "origin": _call_origin(sources.get(receiver) or via, local_modules),
            },
        )
        entry["count"] = int(entry["count"]) + 1
        entry["first_line"] = min(int(entry["first_line"]), node.lineno)
    return found


def _parameter_entry(argument: ast.arg, default: ast.expr | None, kind: str) -> dict[str, Any]:
    """One parameter, with annotation and default rendered as written."""

    entry: dict[str, Any] = {"name": argument.arg, "kind": kind}
    if argument.annotation is not None:
        entry["annotation"] = ast.unparse(argument.annotation)
    if default is not None:
        rendered = ast.unparse(default)
        entry["default"] = rendered if len(rendered) <= 60 else "…"
    return entry


def _signatures(tree: ast.Module) -> dict[str, dict[str, Any]]:
    """Each function's parameters, with annotations and defaults as written.

    A caller needs the signature, and until now the ledger held only the name.
    Whether a parameter is required, what type it declares, and what it falls
    back to are the three things that decide how a function is called, and
    none of them were recorded anywhere.

    Defaults are rendered from source rather than evaluated, so a mutable
    default stays visible as the expression it is.
    """

    found: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        arguments = node.args
        positional = [*arguments.posonlyargs, *arguments.args]
        # Defaults bind to the tail of the positional list.
        offset = len(positional) - len(arguments.defaults)
        entries: list[dict[str, Any]] = []

        for index, argument in enumerate(positional):
            default = (
                arguments.defaults[index - offset]
                if index >= offset and arguments.defaults
                else None
            )
            entries.append(_parameter_entry(argument, default, "positional"))
        if arguments.vararg is not None:
            entries.append(_parameter_entry(arguments.vararg, None, "var_positional"))
        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
            entries.append(_parameter_entry(argument, default, "keyword_only"))
        if arguments.kwarg is not None:
            entries.append(_parameter_entry(arguments.kwarg, None, "var_keyword"))

        if entries:
            found[node.name] = {
                "parameters": entries,
                "line": node.lineno,
                "returns": ast.unparse(node.returns) if node.returns is not None else None,
            }
    return found


def _defined_exceptions(tree: ast.Module) -> list[tuple[str, str, int]]:
    """`(name, base, line)` for exception types a module declares.

    A package's own exception types are its error contract: they say what a
    caller is expected to catch, and what a maintainer may not rename without
    breaking one. Nothing here recorded them, because the only `try` handling
    this analyzer had was written to recognise one fixture's AI-client
    fallbacks -- a rule about a repository rather than about Python.
    """

    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            name = base.id if isinstance(base, ast.Name) else ""
            if name.endswith(("Error", "Exception")):
                found.append((node.name, name, node.lineno))
                break
    return found


def _caught_families(tree: ast.Module) -> list[tuple[str, int]]:
    """The exception family each `except` clause names, in source order.

    A bare `except:` is recorded as `*`, because catching everything is a
    different fact from catching something and is worth being able to find.
    """

    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            found.append(("*", node.lineno))
            continue
        parts = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        rendered = ", ".join(ast.unparse(item) for item in parts)
        found.append((rendered, node.lineno))
    return found


def _reference_literals(tree: ast.Module) -> set[int]:
    """Constant nodes that name a string rather than address a service.

    A URL-shaped literal is not automatically an endpoint the program calls.
    Three positions make it something else, and all three occur in this
    repository:

    * **A pattern being matched.** `"http://localhost:8000" in token.value`
      is a detector. Reporting it as a hardcoded endpoint says the analyzer
      dials the address it was written to find.
    * **A schema or namespace identifier.** A `$schema` value is a name that
      happens to look like a location; nothing fetches it.
    * **A comparison operand.** `if host == "https://x.test/y"` tests a value
      rather than contacting one.

    Returned as node identities because `ast` exposes no parent links, so
    position has to be recorded while the parent is still in hand.
    """

    found: set[int] = set()

    def mark(node: ast.expr | None) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(id(node))

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            mark(node.left)
            for operand in node.comparators:
                mark(operand)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in STRING_MATCH_METHODS:
                for argument in node.args:
                    mark(argument)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if isinstance(key, ast.Constant) and key.value in IDENTIFIER_KEYS:
                    mark(value)
    return found


def _declared_cli_flags(tree: ast.Module) -> tuple[dict[str, int], dict[str, int]]:
    """Options and positional arguments an argparse parser declares.

    A `__main__` guard says a module can be started. It does not say what a
    person types after the module name, and that is the whole interface: this
    engine's own specification of itself named none of its 106 flags, so the
    document could not answer "how do I run this" about the tool that wrote it.

    Only `add_argument` with a string literal first argument is read. A flag
    assembled at run time has no fixed spelling to report, and a guess about a
    command line is worse than an omission, because somebody will type what
    the document says.

    Click and Typer declare the same thing through decorators and are not read
    here. Neither appears in any repository available to check against, and a
    reader written against an imagined codebase is the failure this project
    measures itself on.

    Each name is returned with the line that declares it, so a flag can join
    the concordance on the same terms as every other name a file carries.
    """

    options: dict[str, int] = {}
    positionals: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not isinstance(function, ast.Attribute) or function.attr != "add_argument":
            continue
        line = getattr(node, "lineno", 1)
        for argument in node.args:
            if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
                continue
            name = argument.value.strip()
            if not name:
                continue
            target = options if name.startswith("-") else positionals
            target[name] = min(target.get(name, line), line)
    return options, positionals


# A vocabulary written out as literal strings. The same set is routinely
# declared in several places -- a SQL CHECK, a runtime guard, a schema enum, a
# type annotation -- and nothing makes them move together.
VALUE_SET_KINDS = ("literal_type", "enum_class", "cli_choices", "membership_guard")
MIN_VALUE_SET_MEMBERS = 2


def _string_members(node: ast.expr | None) -> tuple[str, ...]:
    """Every element of a literal collection, when all of them are strings.

    A collection with one non-literal element is not a closed vocabulary, and
    reporting the literal part of it would name a set the code never uses.
    """

    if not isinstance(node, ast.Tuple | ast.List | ast.Set):
        return ()
    members: list[str] = []
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return ()
        members.append(element.value)
    return tuple(members)


def _literal_members(node: ast.expr | None) -> tuple[str, ...]:
    """Members of a `Literal[...]` annotation, ignoring any `| None` union."""

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _literal_members(node.left) or _literal_members(node.right)
    if not isinstance(node, ast.Subscript):
        return ()
    base = node.value
    name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
    if name != "Literal":
        return ()
    index = node.slice
    elements = index.elts if isinstance(index, ast.Tuple) else [index]
    members: list[str] = []
    for element in elements:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return ()
        members.append(element.value)
    return tuple(members)


def _declared_value_sets(tree: ast.Module) -> list[dict[str, Any]]:
    """Closed vocabularies this module states, with where and how each is written.

    Four spellings, all of them literal in source. A `Literal[...]` annotation
    names the values a parameter accepts; a `str, Enum` class names them as
    members; `choices=` names them on a command line; and `x not in {...}`
    names them in a guard that raises.

    The point is not any one of these. It is that a project usually writes the
    same vocabulary in more than one of them, and changing one leaves the rest
    stale with nothing to notice. Recovering each with its own receipt is what
    lets a later join say they agree, or say precisely how they do not.
    """

    found: list[dict[str, Any]] = []

    def record(label: str, members: tuple[str, ...], kind: str, line: int) -> None:
        if len(set(members)) < MIN_VALUE_SET_MEMBERS:
            return
        found.append(
            {
                "label": label,
                "members": sorted(set(members)),
                "kind": kind,
                "line": line,
            }
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = {
                base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                for base in node.bases
            }
            if "Enum" in bases or "StrEnum" in bases:
                members = tuple(
                    statement.value.value
                    for statement in node.body
                    if isinstance(statement, ast.Assign)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                )
                record(node.name, members, "enum_class", node.lineno)
            continue

        if isinstance(node, ast.arg) and node.annotation is not None:
            members = _literal_members(node.annotation)
            if members:
                record(node.arg, members, "literal_type", node.lineno)
            continue

        if isinstance(node, ast.AnnAssign) and node.annotation is not None:
            members = _literal_members(node.annotation)
            if members:
                target = _expr_name(node.target) or "value"
                record(target, members, "literal_type", node.lineno)
            continue

        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg != "choices":
                    continue
                members = _string_members(keyword.value)
                if not members:
                    continue
                first = node.args[0] if node.args else None
                label = (
                    first.value.lstrip("-").replace("-", "_")
                    if isinstance(first, ast.Constant) and isinstance(first.value, str)
                    else "choices"
                )
                record(label, members, "cli_choices", node.lineno)
            continue

        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            if not isinstance(node.ops[0], ast.In | ast.NotIn):
                continue
            members = _string_members(node.comparators[0])
            if not members:
                continue
            label = _expr_name(node.left) or "value"
            record(label.rsplit(".", 1)[-1], members, "membership_guard", node.lineno)
    return found


def _embedded_literals(tree: ast.Module) -> dict[str, dict[str, Any]]:
    """Numeric literals written inside a function body.

    A number given a module-level name is a tunable and already reported. A
    number written straight into the logic — a 300-second buff, a 1500ms
    cooldown, a 0.15 drop chance — is the same decision made without a name,
    and it is the harder one to find: nothing indexes it, and changing the
    behaviour means locating every site by reading.

    0 and 1 are excluded because they are structural far more often than they
    are decisions; every other value is recorded with the line that holds it.
    """

    def own_body(node: ast.AST) -> list[ast.AST]:
        """Nodes belonging to this function, stopping at a nested definition.

        ast.walk cannot express this: it queues a node's children before the
        caller sees the node, so skipping a nested FunctionDef still yields
        everything inside it.
        """

        collected: list[ast.AST] = []
        pending: list[ast.AST] = list(ast.iter_child_nodes(node))
        while pending:
            current = pending.pop()
            if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            collected.append(current)
            pending.extend(ast.iter_child_nodes(current))
        return collected

    def render(node: ast.AST) -> str | None:
        """The literal as written, so a float keeps the point that says so."""

        sign = ""
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            sign, node = "-", node.operand
        if not isinstance(node, ast.Constant):
            return None
        value = node.value
        # bool is an int subclass, and True is not a magic number.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if value in {0, 1}:
            return None
        return f"{sign}{value!r}"

    found: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        values: dict[str, int] = {}
        for inner in own_body(node):
            rendered = render(inner)
            if rendered is None:
                continue
            line = getattr(inner, "lineno", node.lineno)
            # Traversal order is not source order, so the earliest line has to
            # be taken rather than the first one seen.
            values[rendered] = min(values.get(rendered, line), line)
        if values:
            found[node.name] = {
                "values": [
                    {"value": value, "line": line} for value, line in sorted(values.items())
                ],
                "line": node.lineno,
            }
    return found


def _string_constants(tree: ast.Module) -> dict[str, dict[str, Any]]:
    """Module-level string constants, with their values.

    The tunable index records numbers because a number is obviously a dial.
    A string constant is one too: `_TELEGRAPH_ENRAGE = "ANNIHILATE"` names a
    behaviour the rest of the system compares against, and a reader who cannot
    see the value cannot match it to the data it will meet at runtime.

    Docstrings are excluded — a module's own prose is not a constant it uses.
    """

    found: dict[str, dict[str, Any]] = {}
    for statement in tree.body:
        targets: list[ast.expr]
        if isinstance(statement, ast.AnnAssign):
            targets = [statement.target] if statement.value is not None else []
            value: ast.expr | None = statement.value
        elif isinstance(statement, ast.Assign):
            targets, value = list(statement.targets), statement.value
        else:
            continue
        literal = _literal_string(value)
        if literal is None or "\n" in literal or len(literal) > 120:
            continue
        for name in (n for target in targets for n in _assigned_names(target)):
            found.setdefault(name, {"value": literal, "line": statement.lineno})
    return found


def _imported_names(tree: ast.Module) -> dict[str, dict[str, Any]]:
    """Which names each module contributes, not merely that it was imported.

    An import edge records that `fastapi` is used. It does not record that what
    is used from it is `Depends`, and the difference decides whether a
    dependency is load-bearing or incidental: a module importing one helper
    from a framework is in a different position than one importing its router,
    its middleware and its exception handlers.
    """

    found: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # A relative import has no module name at level > 0; the dots are
            # the target, so they are kept rather than dropped.
            module = "." * node.level + (node.module or "")
            names = [alias.asname or alias.name for alias in node.names]
        elif isinstance(node, ast.Import):
            module = ""
            names = [alias.asname or alias.name for alias in node.names]
        else:
            continue
        if not module or not names:
            continue
        entry = found.setdefault(module, {"names": [], "line": node.lineno})
        for name in names:
            if name not in entry["names"]:
                entry["names"].append(name)
        entry["line"] = min(int(entry["line"]), node.lineno)
    for entry in found.values():
        entry["names"] = sorted(entry["names"])
    return found


def _model_fields(tree: ast.Module) -> dict[str, dict[str, Any]]:
    """Annotated class attributes: the data contract a codebase declares outright.

    A Pydantic model, a dataclass or a plain annotated class is where a system
    writes down the shape of what it stores and returns. Those fields are not
    functions, so a symbol index skips them, and they are not columns, so an
    ERD built from SQL skips them too — a repository that persists JSON blobs
    keeps its entire schema here and nowhere else.

    The annotation is recorded as written rather than resolved, because the
    declared type is the contract; what it evaluates to at import time is not
    knowable without running the code.
    """

    found: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        fields: list[dict[str, Any]] = []
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(
                statement.target, ast.Name
            ):
                continue
            entry: dict[str, Any] = {
                "name": statement.target.id,
                "annotation": ast.unparse(statement.annotation),
                "line": statement.lineno,
                "required": statement.value is None,
            }
            if statement.value is not None:
                rendered = ast.unparse(statement.value)
                # A long default is a value, not a signal; the line locates it.
                entry["default"] = rendered if len(rendered) <= 60 else "…"
            fields.append(entry)
        if fields:
            bases = [ast.unparse(base) for base in node.bases]
            found[node.name] = {"fields": fields, "line": node.lineno, "bases": bases}
    return found


def _payload_shapes(tree: ast.Module) -> dict[str, dict[str, Any]]:
    """Field names of dictionaries a function returns.

    Where a service serialises dictionaries rather than declaring models, the
    response contract exists only as the keys of the dict literals it returns.
    A caller integrating against it needs those names, and no symbol index or
    table schema contains them — the payload is a JSON blob as far as storage
    is concerned.

    Only literal string keys are recorded, so a key computed at runtime is
    absent rather than guessed at.
    """

    found: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        keys: set[str] = set()
        line: int | None = None
        for statement in ast.walk(node):
            if not isinstance(statement, ast.Return) or statement.value is None:
                continue
            candidates = [statement.value]
            # `return x if cond else y` returns whichever shape the branch picks.
            if isinstance(statement.value, ast.IfExp):
                candidates = [statement.value.body, statement.value.orelse]
            for candidate in candidates:
                if not isinstance(candidate, ast.Dict):
                    continue
                literal = {
                    key.value
                    for key in candidate.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                if literal:
                    keys |= literal
                    line = statement.lineno if line is None else min(line, statement.lineno)
        if keys and line is not None:
            found[node.name] = {"fields": sorted(keys), "line": line}
    return found


def _module_mutable_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and _is_mutable_initializer(statement.value):
            names.update(name for target in statement.targets for name in _assigned_names(target))
        elif isinstance(statement, ast.AnnAssign) and _is_mutable_initializer(statement.value):
            names.update(_assigned_names(statement.target))
    return names


class _DirectBindingCollector(ast.NodeVisitor):
    """Collect names bound by one function without descending into nested scopes."""

    def __init__(self) -> None:
        self.bound: set[str] = set()
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bound.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        self.bound.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.bound.update(alias.asname or alias.name for alias in node.names if alias.name != "*")

    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.bound.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.bound.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bound.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: ARG002
        # A lambda opens its own scope, so its bindings are not the enclosing
        # function's. Deliberately not visited.
        return


def _function_bindings(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> tuple[set[str], set[str]]:
    collector = _DirectBindingCollector()
    arguments = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
    if node.args.vararg:
        arguments.append(node.args.vararg)
    if node.args.kwarg:
        arguments.append(node.args.kwarg)
    collector.bound.update(argument.arg for argument in arguments)
    body = node.body if isinstance(node.body, list) else [node.body]
    for statement in body:
        collector.visit(statement)
    collector.bound.difference_update(collector.global_names)
    collector.bound.update(collector.nonlocal_names)
    return collector.bound, collector.global_names


class _ModuleMutationCollector(ast.NodeVisitor):
    """Find concrete mutations of module-owned mutable containers."""

    def __init__(self, module_mutables: set[str]) -> None:
        self.module_mutables = module_mutables
        self.scope_bindings: list[set[str]] = []
        self.scope_globals: list[set[str]] = []
        self.mutations: dict[str, list[ast.AST]] = {name: [] for name in module_mutables}

    def _is_module_reference(self, name: str) -> bool:
        for bound, declared_global in zip(
            reversed(self.scope_bindings), reversed(self.scope_globals), strict=True
        ):
            if name in declared_global:
                return True
            if name in bound:
                return False
        return name in self.module_mutables

    def _record_target(self, target: ast.AST, evidence_node: ast.AST) -> None:
        candidate: ast.AST = target
        while isinstance(candidate, (ast.Subscript, ast.Attribute)):
            candidate = candidate.value
        if isinstance(candidate, ast.Name) and self._is_module_reference(candidate.id):
            # `setdefault` rather than indexing, because the two paths into
            # this branch do not agree on which names exist. The map is keyed
            # on module-owned mutable containers, while `_is_module_reference`
            # also answers yes to anything an enclosing scope declared
            # `global`. A module-level counter is the common case -- an int is
            # not a container, so `global n` followed by `n += 1` reached a key
            # that was never created and raised `KeyError`, aborting the whole
            # analysis of the repository rather than skipping one statement.
            #
            # Recording it is also the right answer. A counter rebound across
            # calls through `global` is process-local state by the plainest
            # reading of the term.
            self.mutations.setdefault(candidate.id, []).append(evidence_node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        bound, declared_global = _function_bindings(node)
        self.scope_bindings.append(bound)
        self.scope_globals.append(declared_global)
        body = node.body if isinstance(node.body, list) else [node.body]
        for statement in body:
            self.visit(statement)
        self.scope_globals.pop()
        self.scope_bindings.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_function(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if not isinstance(target, ast.Name):
                self._record_target(target, node)
        self.generic_visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not isinstance(node.target, ast.Name):
            self._record_target(node.target, node)
        if node.value:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_target(node.target, node)
        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._record_target(target, node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in MUTATING_METHODS:
            self._record_target(node.func.value, node)
        self.generic_visit(node)


class _PythonFileAnalyzer(ast.NodeVisitor):
    def __init__(
        self,
        *,
        snapshot: Snapshot,
        file_record: FileRecord,
        source: str,
        tree: ast.Module,
        created_at: str,
        module: str,
        local_modules: frozenset[str] = frozenset(),
    ) -> None:
        self.snapshot = snapshot
        self.file_record = file_record
        self.path = file_record.path
        self.module = module
        self.source = source
        self.source_lines = source.splitlines(keepends=True)
        self.tree = tree
        self.created_at = created_at
        self.symbols: list[SymbolRecord] = []
        self.edges: list[EdgeRecord] = []
        self.evidence: list[EvidenceRecord] = []
        self.claims: list[ClaimRecord] = []
        self.scope_names: list[str] = [self.module]
        self.scope_ids: list[str] = []
        # One value per lexical scope. A request method is a route only when
        # its immediate owner is a proven ``http.server`` handler class. The
        # false value pushed for functions prevents a nested ``do_GET`` helper
        # from inheriting the surrounding class's transport role.
        self.stdlib_http_handler_scopes: list[bool] = [False]
        self.test_evidence: list[str] = []
        self.route_evidence: list[str] = []
        self.route_path_literals: set[str] = set()
        self.endpoint_literals: set[str] = set()
        self.reference_literals = _reference_literals(tree)
        self.endpoint_evidence: list[str] = []
        self.route_auth_control_evidence: list[str] = []
        self.typed_route_evidence: list[str] = []
        self.exit_evidence: list[tuple[int | None, str]] = []
        self.numeric_constants: dict[str, float] = {}
        self.constant_lines: dict[str, int] = {}
        mutation_collector = _ModuleMutationCollector(_module_mutable_names(tree))
        mutation_collector.visit(tree)
        self.module_mutations = mutation_collector.mutations

        module_end = max(1, file_record.line_count)
        self.state_fields = _state_fields(tree)
        self.instance_tunables = _instance_tunables(tree)
        self.data_containers = _data_containers(tree)
        self.payload_shapes = _payload_shapes(tree)
        self.model_fields = _model_fields(tree)
        self.imported_names = _imported_names(tree)
        self.string_constants = _string_constants(tree)
        self.embedded_literals = _embedded_literals(tree)
        self.value_sets = _declared_value_sets(tree)
        cli_options, cli_positionals = _declared_cli_flags(tree)
        self.cli_options = sorted(cli_options)
        self.cli_positionals = sorted(cli_positionals)
        self.defined_exceptions = _defined_exceptions(tree)
        self.caught_families = _caught_families(tree)
        self.caught_family_evidence: dict[str, list[str]] = {}
        self.signatures = _signatures(tree)
        self.external_calls = _external_calls(tree, local_modules)
        self.loop_worksets = _loop_worksets(tree)
        self.name_index = _name_index(tree)
        # `--output-dir` is not a Python identifier, so the generic walk skips
        # it, and a reader searching for a flag found nothing. It is still a
        # name a person types and looks up.
        for flag, line in (*cli_options.items(), *cli_positionals.items()):
            self.name_index[flag] = min(self.name_index.get(flag, line), line)
        module_symbol = self._symbol(
            qualified_name=self.module,
            kind="module",
            start_line=1,
            end_line=module_end,
            metadata={},
        )
        self.scope_ids.append(module_symbol.symbol_id)
        self._evidence(
            start_line=1,
            end_line=module_end,
            symbol=self.module,
            evidence_kind="module",
        )

    @property
    def current_symbol_id(self) -> str:
        return self.scope_ids[-1]

    @property
    def current_qualified_name(self) -> str:
        return ".".join(self.scope_names)

    def _symbol(
        self,
        *,
        qualified_name: str,
        kind: str,
        start_line: int,
        end_line: int,
        metadata: dict[str, Any],
    ) -> SymbolRecord:
        symbol_id = stable_id(
            "symbol",
            (
                self.snapshot.snapshot_id,
                self.path,
                qualified_name,
                kind,
                start_line,
                end_line,
                ANALYZER_VERSION,
            ),
        )
        record = SymbolRecord(
            symbol_id=symbol_id,
            snapshot_id=self.snapshot.snapshot_id,
            path=self.path,
            qualified_name=qualified_name,
            kind=kind,
            start_line=start_line,
            end_line=end_line,
            language="Python",
            analyzer=ANALYZER_VERSION,
            metadata=metadata,
        )
        self.symbols.append(record)
        return record

    def _excerpt_sha256(self, start_line: int | None, end_line: int | None) -> str | None:
        if start_line is None or end_line is None:
            return None
        excerpt = "".join(self.source_lines[start_line - 1 : end_line])
        return hashlib.sha256(excerpt.encode("utf-8")).hexdigest()

    def _evidence(
        self,
        *,
        start_line: int | None,
        end_line: int | None,
        symbol: str | None,
        evidence_kind: str,
    ) -> EvidenceRecord:
        evidence_id = stable_id(
            "evidence",
            (
                self.snapshot.snapshot_id,
                self.path,
                start_line,
                end_line,
                symbol,
                evidence_kind,
                ANALYZER_VERSION,
            ),
        )
        record = EvidenceRecord(
            evidence_id=evidence_id,
            snapshot_id=self.snapshot.snapshot_id,
            path=self.path,
            start_line=start_line,
            end_line=end_line,
            symbol=symbol,
            evidence_kind=evidence_kind,
            excerpt_sha256=self._excerpt_sha256(start_line, end_line),
            analyzer=ANALYZER_VERSION,
            created_at=self.created_at,
        )
        self.evidence.append(record)
        return record

    def _claim(
        self,
        *,
        text: str,
        category: str,
        status: str,
        confidence: float,
        importance: str,
        supporting: tuple[str, ...],
        contradicting: tuple[str, ...] = (),
        invalidation_keys: tuple[str, ...] = (),
        alternatives: tuple[str, ...] = (),
    ) -> ClaimRecord:
        # Re-filing happens here rather than at each call site because doing it
        # per category is how the same mistake kept reappearing: routes were
        # fixed, then schemas, and durable storage was still six-sevenths test
        # fixtures. One table, one place, and a category added later inherits
        # the behaviour instead of having to remember it.
        if str(self.file_record.role) == "test":
            category = TEST_SCOPED_CATEGORIES.get(category, category)
            if category.startswith("test_") and importance == "high":
                importance = "medium"
        claim_id = stable_id(
            "claim",
            (self.snapshot.snapshot_id, category, text, ANALYZER_VERSION),
        )
        record = ClaimRecord(
            claim_id=claim_id,
            snapshot_id=self.snapshot.snapshot_id,
            claim=text,
            category=category,
            status=status,
            confidence=confidence,
            importance=importance,
            produced_by=ANALYZER_VERSION,
            created_at=self.created_at,
            verified_at=self.created_at if status == "verified" else None,
            supporting_evidence=supporting,
            contradicting_evidence=contradicting,
            invalidation_keys=invalidation_keys or (f"file:{self.path}",),
            alternative_hypotheses=alternatives,
        )
        self.claims.append(record)
        return record

    def _edge(
        self,
        *,
        relationship: str,
        target_ref: str,
        evidence_id: str | None,
        source_symbol_id: str | None = None,
        target_symbol_id: str | None = None,
    ) -> EdgeRecord:
        source_id = source_symbol_id or self.current_symbol_id
        edge_id = stable_id(
            "edge",
            (
                self.snapshot.snapshot_id,
                source_id,
                relationship,
                target_ref,
                evidence_id,
                ANALYZER_VERSION,
            ),
        )
        record = EdgeRecord(
            edge_id=edge_id,
            snapshot_id=self.snapshot.snapshot_id,
            source_symbol_id=source_id,
            source_path=self.path,
            relationship=relationship,
            target_ref=target_ref,
            target_symbol_id=target_symbol_id,
            evidence_id=evidence_id,
            analyzer=ANALYZER_VERSION,
        )
        self.edges.append(record)
        return record

    def _routes(self, decorators: list[ast.expr]) -> list[dict[str, Any]]:
        routes: list[dict[str, Any]] = []
        for decorator in decorators:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            method = decorator.func.attr.casefold()
            if method not in HTTP_METHODS:
                continue
            path = _literal_string(decorator.args[0]) if decorator.args else None
            routes.append(
                {
                    "method": method.upper(),
                    "path": path or "<dynamic>",
                    "start_line": getattr(decorator, "lineno", None),
                    "end_line": getattr(
                        decorator, "end_lineno", getattr(decorator, "lineno", None)
                    ),
                    "owner": _expr_name(decorator.func.value),
                    "framework": "decorator",
                    # Carried so the endpoint catalog can separate the served
                    # surface from routes a test registers to exercise it.
                    "role": str(self.file_record.role),
                }
            )
        return routes

    def _stdlib_routes(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict[str, Any]]:
        if not self.stdlib_http_handler_scopes[-1] or not node.name.startswith("do_"):
            return []
        method = node.name.removeprefix("do_").casefold()
        if method not in HTTP_METHODS:
            return []
        return [
            {
                "method": method.upper(),
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
                "owner": "http.server",
                "framework": "http.server",
                "role": str(self.file_record.role),
            }
            for path, start_line, end_line in _route_literals(node)
        ]

    def _visit_definition(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        qualified = f"{self.current_qualified_name}.{node.name}"
        kind = (
            "class"
            if isinstance(node, ast.ClassDef)
            else "async_function"
            if isinstance(node, ast.AsyncFunctionDef)
            else "function"
        )
        routes = self._routes(node.decorator_list)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            routes.extend(self._stdlib_routes(node))
        metadata: dict[str, Any] = {}
        if routes:
            metadata["routes"] = routes
        if not isinstance(node, ast.ClassDef):
            flow = _control_flow(node)
            # A function with no branch has no decision worth drawing; keeping
            # the trace off those symbols keeps the ledger proportional.
            if routes or sum(1 for item in flow if item["kind"] == "guard") >= 2:
                metadata["control_flow"] = flow
        symbol = self._symbol(
            qualified_name=qualified,
            kind=kind,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            metadata=metadata,
        )
        symbol_evidence = self._evidence(
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            symbol=qualified,
            evidence_kind="symbol",
        )
        self._edge(
            relationship="contains",
            target_ref=qualified,
            target_symbol_id=symbol.symbol_id,
            evidence_id=symbol_evidence.evidence_id,
        )

        for route in routes:
            route_evidence = self._evidence(
                start_line=route["start_line"],
                end_line=route["end_line"],
                symbol=qualified,
                evidence_kind="http_route",
            )
            self.route_evidence.append(route_evidence.evidence_id)
            # Only functions carry a signature; a decorated class exposes no
            # parameters to inspect for auth dependencies or type annotations.
            arguments = (
                node.args if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
            )
            signature_nodes: list[ast.AST] = [*node.decorator_list]
            if arguments is not None:
                signature_nodes.extend(arguments.defaults)
                signature_nodes.extend(default for default in arguments.kw_defaults if default)
            has_auth_control = any(
                isinstance(candidate, ast.Call)
                and (_expr_name(candidate.func) or "").split(".")[-1] in {"Depends", "Security"}
                for signature_node in signature_nodes
                for candidate in ast.walk(signature_node)
            )
            if has_auth_control:
                self.route_auth_control_evidence.append(route_evidence.evidence_id)
            typed_parameters = (
                [
                    argument
                    for argument in (
                        *arguments.posonlyargs,
                        *arguments.args,
                        *arguments.kwonlyargs,
                    )
                    if argument.annotation is not None
                ]
                if arguments is not None
                else []
            )
            if typed_parameters and route.get("framework") == "decorator":
                signature_evidence = self._evidence(
                    start_line=node.lineno,
                    end_line=node.lineno,
                    symbol=qualified,
                    evidence_kind="typed_route_signature",
                )
                self.typed_route_evidence.append(signature_evidence.evidence_id)
            # A route registered inside a test is real syntax and not part of
            # the served surface. On a repository whose routes all live in one
            # application module the distinction never shows; on a library with
            # a large test suite it is most of the census, and reporting a test
            # fixture as an endpoint misdescribes what the system exposes.
            in_test = str(self.file_record.role) == "test"
            self._claim(
                text=(
                    f"{route['method']} {route['path']} is registered by {qualified} "
                    "inside a test file, so it exercises the framework rather than "
                    "forming part of the served surface."
                    if in_test
                    else f"{route['method']} {route['path']} is handled by {qualified}."
                ),
                category="test_route" if in_test else "http_route",
                status="verified",
                confidence=1.0,
                importance="low" if in_test else "medium",
                supporting=(route_evidence.evidence_id,),
                invalidation_keys=(f"file:{self.path}", f"symbol:{qualified}"),
            )

        is_test = node.name.startswith("test_") or (
            isinstance(node, ast.ClassDef) and node.name.startswith("Test")
        )
        if is_test:
            self.test_evidence.append(symbol_evidence.evidence_id)

        self.scope_names.append(node.name)
        self.scope_ids.append(symbol.symbol_id)
        handler_scope = isinstance(node, ast.ClassDef) and any(
            (_expr_name(base) or "").rsplit(".", 1)[-1] in STDLIB_HTTP_HANDLER_BASES
            for base in node.bases
        )
        self.stdlib_http_handler_scopes.append(handler_scope)
        self.generic_visit(node)
        self.stdlib_http_handler_scopes.pop()
        self.scope_ids.pop()
        self.scope_names.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_definition(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_definition(node)

    def visit_Import(self, node: ast.Import) -> None:
        evidence = self._evidence(
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            symbol=self.current_qualified_name,
            evidence_kind="import",
        )
        for alias in node.names:
            self._edge(
                relationship="imports",
                target_ref=alias.name,
                evidence_id=evidence.evidence_id,
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        evidence = self._evidence(
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            symbol=self.current_qualified_name,
            evidence_kind="import",
        )
        module = "." * node.level + (node.module or "")
        for alias in node.names:
            target = f"{module}.{alias.name}".strip(".")
            self._edge(
                relationship="imports",
                target_ref=target,
                evidence_id=evidence.evidence_id,
            )

    def _module_assignment(self, node: ast.Assign | ast.AnnAssign) -> None:
        if len(self.scope_names) != 1:
            return
        value = node.value
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [name for target in targets for name in _assigned_names(target)]
        if (
            "__all__" in names
            and isinstance(value, (ast.List, ast.Tuple))
            and describes_the_product(getattr(self.file_record, "role", None))
        ):
            exported = tuple(
                item.value
                for item in value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
            if exported:
                surface = self._evidence(
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    symbol=self.module,
                    evidence_kind="public_api",
                )
                self._claim(
                    text=(
                        f"{self.module} declares {len(exported)} name(s) as its public surface: "
                        f"{', '.join(sorted(exported)[:12])}"
                        f"{'...' if len(exported) > 12 else ''}. Removing or renaming one is a "
                        "breaking change for every importer."
                    ),
                    category="public_api",
                    status="verified",
                    confidence=1.0,
                    importance="high",
                    supporting=(surface.evidence_id,),
                    invalidation_keys=(f"file:{self.path}", f"symbol:{self.module}.__all__"),
                )
        for name in names:
            qualified = f"{self.module}.{name}"
            symbol = self._symbol(
                qualified_name=qualified,
                kind="module_variable",
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                metadata={"mutable_initializer": _is_mutable_initializer(value)},
            )
            evidence = self._evidence(
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                symbol=qualified,
                evidence_kind="assignment",
            )
            self._edge(
                relationship="contains",
                target_ref=qualified,
                target_symbol_id=symbol.symbol_id,
                evidence_id=evidence.evidence_id,
            )

            call_name = _expr_name(value.func) if isinstance(value, ast.Call) else None
            numeric_value = _literal_number(value)
            if numeric_value is not None:
                self.numeric_constants[name] = numeric_value
                self.constant_lines.setdefault(name, node.lineno)
            if (
                call_name
                and call_name.split(".")[-1] == "FastAPI"
                and describes_the_product(getattr(self.file_record, "role", None))
            ):
                self._claim(
                    text=f"{qualified} constructs a FastAPI application.",
                    category="application_entry",
                    status="verified",
                    confidence=1.0,
                    importance="high",
                    supporting=(evidence.evidence_id,),
                    invalidation_keys=(f"file:{self.path}", f"symbol:{qualified}"),
                )
            mutation_nodes = self.module_mutations.get(name, [])
            if _is_mutable_initializer(value) and mutation_nodes:
                mutation_evidence = tuple(
                    self._evidence(
                        start_line=getattr(mutation, "lineno", None),
                        end_line=getattr(mutation, "end_lineno", getattr(mutation, "lineno", None)),
                        symbol=qualified,
                        evidence_kind="mutation",
                    ).evidence_id
                    for mutation in mutation_nodes
                )
                self._claim(
                    text=(
                        f"{qualified} is a module-owned mutable container with observed mutation "
                        "sites; its contents are process-local unless code outside this module "
                        "synchronizes them to durable storage."
                    ),
                    category="process_local_state",
                    status="inferred",
                    confidence=0.9,
                    importance="high",
                    supporting=(evidence.evidence_id, *mutation_evidence),
                    invalidation_keys=(f"file:{self.path}", f"symbol:{qualified}"),
                    alternatives=(
                        "The assigned object may proxy or synchronize state to an external store.",
                    ),
                )
            elif mutation_nodes and not _is_mutable_initializer(value):
                # A scalar is not a container, so the branch above passes over
                # it. `counter = 0` rebound from a function through `global` is
                # still state this process owns and the next one starts without
                # -- counters, cached singletons and feature flags all take this
                # shape. The gate is an observed rebinding rather than the
                # declaration, so a module constant nobody writes to stays a
                # constant.
                rebind_evidence = tuple(
                    self._evidence(
                        start_line=getattr(mutation, "lineno", None),
                        end_line=getattr(mutation, "end_lineno", getattr(mutation, "lineno", None)),
                        symbol=qualified,
                        evidence_kind="rebinding",
                    ).evidence_id
                    for mutation in mutation_nodes
                )
                self._claim(
                    text=(
                        f"{qualified} is a module-level value rebound from "
                        f"{len(mutation_nodes)} site(s) inside functions; its current value is "
                        "process-local, so a second instance of this program starts without it."
                    ),
                    category="process_local_state",
                    status="inferred",
                    confidence=0.9,
                    importance="high",
                    supporting=(evidence.evidence_id, *rebind_evidence),
                    invalidation_keys=(f"file:{self.path}", f"symbol:{qualified}"),
                    alternatives=(
                        (
                            "The value may be re-derived at startup, or written through to a "
                            "store by code this analyzer did not resolve."
                        ),
                    ),
                )

    def visit_Assign(self, node: ast.Assign) -> None:
        self._module_assignment(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._module_assignment(node)
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        if (
            len(self.scope_names) == 1
            and _is_main_guard(node)
            and describes_the_product(getattr(self.file_record, "role", None))
        ):
            evidence = self._evidence(
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                symbol=self.module,
                evidence_kind="entry_point",
            )
            self._claim(
                text=f"{self.path} defines a direct Python __main__ entry point.",
                category="application_entry",
                status="verified",
                confidence=1.0,
                importance="medium",
                supporting=(evidence.evidence_id,),
            )
        for comparison in (
            candidate for candidate in ast.walk(node.test) if isinstance(candidate, ast.Compare)
        ):
            if len(comparison.ops) != 1 or not isinstance(comparison.ops[0], ast.NotIn):
                continue
            if len(comparison.comparators) != 1:
                continue
            field = _expr_name(comparison.left)
            registry = _expr_name(comparison.comparators[0])
            if not field or not registry:
                continue
            clears_field = any(
                isinstance(candidate, (ast.Assign, ast.AnnAssign))
                and isinstance(candidate.value, ast.Constant)
                and candidate.value.value is None
                and field
                in [
                    _expr_name(target)
                    for target in (
                        candidate.targets
                        if isinstance(candidate, ast.Assign)
                        else [candidate.target]
                    )
                ]
                for statement in node.body
                for candidate in ast.walk(statement)
            )
            if clears_field:
                evidence = self._evidence(
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    symbol=self.current_qualified_name,
                    evidence_kind="state_reconciliation",
                )
                self._claim(
                    text=(
                        f"{self.current_qualified_name} clears {field} when it is absent from "
                        f"{registry}, explicitly reconciling a pointer with a process registry."
                    ),
                    category="state_reconciliation",
                    status="verified",
                    confidence=1.0,
                    importance="high",
                    supporting=(evidence.evidence_id,),
                )
        self.generic_visit(node)

    def _collection_workset(self, node: ast.For | ast.AsyncFor) -> None:
        collection = self.loop_worksets.get(id(node)) or _iterated_collection(node.iter)
        imported = {
            str(name) for entry in self.imported_names.values() for name in entry.get("names", ())
        }
        if not collection or collection.split(".", maxsplit=1)[0] not in imported:
            return
        # A public iterable is an ordinary module contract. Reaching into an
        # underscore member is the unusual boundary: the caller's work now
        # depends on an internal collection the provider is free to change.
        if not any(part.startswith("_") for part in collection.split(".")[1:]):
            return
        evidence = self._evidence(
            start_line=node.lineno,
            end_line=node.lineno,
            symbol=self.current_qualified_name,
            evidence_kind="collection_driven_workset",
        )
        self._claim(
            text=(
                f"{self.current_qualified_name} iterates over the imported private collection "
                f"`{collection}`; membership in that collection defines this loop's work set, "
                "so values not resident in it receive no work from this loop."
            ),
            category="collection_driven_workset",
            status="verified",
            confidence=1.0,
            importance="high",
            supporting=(evidence.evidence_id,),
            invalidation_keys=(f"file:{self.path}", f"symbol:{self.current_qualified_name}"),
        )

    def visit_For(self, node: ast.For) -> None:
        self._collection_workset(node)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._collection_workset(node)
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Pow):
            base = _literal_number(node.left)
            if base is None and isinstance(node.left, ast.Name):
                base = self.numeric_constants.get(node.left.id)
            exponent = ast.unparse(node.right)
            # A constant base raised to something computed grows exponentially
            # with that something, whatever it is called. This used to require
            # the word "ascension" in the exponent -- a term from the game this
            # analyzer was first written against -- so `1.15 ** level`,
            # `2 ** retries` and `1.5 ** tier` were all invisible, and the
            # category fired for exactly one repository. A literal exponent is
            # excluded because `10 ** 6` is a number, not a growth curve.
            varying = _literal_number(node.right) is None
            if base is not None and varying and exponent:
                evidence = self._evidence(
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    symbol=self.current_qualified_name,
                    evidence_kind="exponential_scaling",
                )
                self._claim(
                    text=(
                        f"{self.current_qualified_name} exponentiates base {base:g} by "
                        f"`{exponent}`, so the result grows exponentially with that value."
                    ),
                    category="exponential_scaling",
                    status="verified",
                    confidence=1.0,
                    importance="high",
                    supporting=(evidence.evidence_id,),
                )
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        call_names = {
            _expr_name(candidate.func) or ""
            for statement in node.body
            for candidate in ast.walk(statement)
            if isinstance(candidate, ast.Call)
        }
        # Any call reached through an import, rather than three method names
        # from the repository this was written against. `ai_client` and
        # `generate_content` are that repository's own spellings, so a handler
        # wrapping `openai`, `requests` or `redis` and swallowing the failure
        # was invisible everywhere else. A call to a local helper is excluded
        # because `try: helper()` returning None is ordinary control flow, not
        # a boundary whose failure disappears.
        absorbed = sorted(name for name in call_names if name in self.external_calls)
        fallback_labels: set[str] = set()
        if absorbed:
            for handler in node.handlers:
                for candidate in ast.walk(handler):
                    if not isinstance(candidate, ast.Return):
                        continue
                    if isinstance(candidate.value, ast.Constant):
                        if candidate.value.value is None:
                            fallback_labels.add("None")
                        elif candidate.value.value == "":
                            fallback_labels.add("empty string")
                    if isinstance(candidate.value, ast.Dict):
                        for value in candidate.value.values:
                            if isinstance(value, ast.Constant) and value.value is None:
                                fallback_labels.add("None")
                            elif isinstance(value, ast.Constant) and value.value == "":
                                fallback_labels.add("empty string")
        if fallback_labels:
            evidence = self._evidence(
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                symbol=self.current_qualified_name,
                evidence_kind="ai_failure_fallback",
            )
            rendered = ", ".join(sorted(fallback_labels))
            named = ", ".join(f"`{item}`" for item in absorbed[:4])
            self._claim(
                text=(
                    f"{self.current_qualified_name} catches around {named} and returns "
                    f"{rendered} on failure, so a caller cannot tell the call failed."
                ),
                category="absorbed_failure",
                status="verified",
                confidence=1.0,
                importance="high",
                supporting=(evidence.evidence_id,),
            )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and ENDPOINT_LITERAL.match(node.value):
            # A literal in a test states what the fixture uses, not what the
            # system contacts, and one in a match position is a pattern. Both
            # were being reported as endpoints this program dials.
            if (
                str(self.file_record.role) != "test"
                and id(node) not in self.reference_literals
                and node.value not in self.endpoint_literals
            ):
                self.endpoint_literals.add(node.value)
                receipt = self._evidence(
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    symbol=self.current_qualified_name,
                    evidence_kind="endpoint_literal",
                )
                self.endpoint_evidence.append(receipt.evidence_id)
            return
        # Route-path literals are how a client or harness names an endpoint it
        # never imports. Recording them lets traceability follow HTTP exercise,
        # not only direct calls.
        if not isinstance(node.value, str) or not ROUTE_PATH_LITERAL.match(node.value):
            return
        if node.value in self.route_path_literals:
            return
        self.route_path_literals.add(node.value)
        evidence = self._evidence(
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            symbol=self.current_qualified_name,
            evidence_kind="route_path_literal",
        )
        self._edge(
            relationship="references_route_path",
            target_ref=node.value,
            evidence_id=evidence.evidence_id,
        )

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _expr_name(node.func)
        # A deprecation is a removal that has been scheduled and announced. It
        # is one of the few facts a library states about its own future, and
        # the reason a caller pinning this version needs to read the section
        # before upgrading rather than after.
        # Matching any `.warn` swept in `logger.warn`, which reports a
        # condition rather than a scheduled removal. The receiver has to be
        # the `warnings` module, or the bare name imported from it.
        if call_name in {"warnings.warn", "warn"}:
            categories = {
                _expr_name(argument)
                for argument in (*node.args, *(keyword.value for keyword in node.keywords))
            }
            named = sorted(
                item.split(".")[-1]
                for item in categories
                if item and item.split(".")[-1] in DEPRECATION_CATEGORIES
            )
            if named:
                notice = self._evidence(
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    symbol=self.current_qualified_name,
                    evidence_kind="deprecation",
                )
                self._claim(
                    text=(
                        f"{self.current_qualified_name} raises {named[0]} at runtime, so callers "
                        "of it are being told this path is scheduled for removal."
                    ),
                    category="deprecation",
                    status="verified",
                    confidence=1.0,
                    importance="high",
                    supporting=(notice.evidence_id,),
                    invalidation_keys=(
                        f"file:{self.path}",
                        f"symbol:{self.current_qualified_name}",
                    ),
                )
        if call_name:
            evidence = self._evidence(
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                symbol=self.current_qualified_name,
                evidence_kind="call",
            )
            self._edge(
                relationship="calls",
                target_ref=call_name,
                evidence_id=evidence.evidence_id,
            )

            normalized = call_name.casefold()
            if normalized in {"os.getenv", "os.environ.get", "environ.get"}:
                key = _literal_string(node.args[0]) if node.args else None
                self._claim(
                    text=(
                        f"{self.current_qualified_name} reads environment setting "
                        f"{key if key is not None else '<dynamic>'}."
                    ),
                    category="configuration_read",
                    status="verified",
                    confidence=1.0,
                    importance="medium",
                    supporting=(evidence.evidence_id,),
                )

            if normalized in {"sys.exit", "exit"}:
                code = None
                if node.args:
                    numeric_code = _literal_number(node.args[0])
                    code = int(numeric_code) if numeric_code is not None else None
                self.exit_evidence.append((code, evidence.evidence_id))

            if normalized.endswith("sqlite3.connect") or normalized == "sqlite3.connect":
                location = _literal_string(node.args[0]) if node.args else None
                self._claim(
                    text=(
                        f"{self.current_qualified_name} opens a SQLite connection"
                        + (
                            f" to {location}."
                            if location
                            else " using a dynamically resolved location."
                        )
                    ),
                    category="storage",
                    status="verified",
                    confidence=1.0,
                    importance="high",
                    supporting=(evidence.evidence_id,),
                )

            if normalized.endswith((".execute", ".executescript")):
                sql = _literal_string(node.args[0]) if node.args else None
                # A schema evolved in code rather than in a migrations directory
                # still evolves. Reporting only directories makes every project
                # that migrates inline look like one that cannot migrate at all,
                # and these statements are routinely built with an f-string.
                shape = _sql_shape(node.args[0]) if node.args else None
                if shape:
                    for match in ALTER_TABLE_PATTERN.finditer(shape):
                        table = match.group("table").strip('"`[]')
                        column = match.group("column").strip('"`[]')
                        where = (
                            "a SQLite table named at runtime"
                            if table == "{}"
                            else f"SQLite table {table}"
                        )
                        what = "a column named at runtime" if column == "{}" else f"column {column}"
                        self._claim(
                            text=(
                                f"{self.current_qualified_name} alters {where} to "
                                f"add {what}, migrating the schema in application "
                                "code rather than through a migration tool."
                            ),
                            category="schema_migration",
                            status="verified",
                            confidence=1.0,
                            importance="high",
                            supporting=(evidence.evidence_id,),
                        )
                if sql:
                    # A table built inside a test is the fixture's shape, not
                    # the system's. Filing both under one category is how a
                    # schema-three ledger written to exercise a migration ends
                    # up listed beside the production tables it stands in for.
                    in_test = str(self.file_record.role) == "test"
                    for match in CREATE_TABLE_PATTERN.finditer(sql):
                        table = match.group(1)
                        self._claim(
                            text=(
                                f"{self.current_qualified_name} creates SQLite table {table} "
                                "as a test fixture."
                                if in_test
                                else f"{self.current_qualified_name} creates SQLite table {table}."
                            ),
                            category="storage_schema",
                            status="verified",
                            confidence=1.0,
                            importance="high",
                            supporting=(evidence.evidence_id,),
                        )
                    insert_match = INSERT_TABLE_PATTERN.search(sql)
                    arguments = node.args[1:] + [keyword.value for keyword in node.keywords]
                    serializes_json = any(
                        isinstance(candidate, ast.Call)
                        and (_expr_name(candidate.func) or "").casefold().endswith("json.dumps")
                        for argument in arguments
                        for candidate in ast.walk(argument)
                    )
                    if insert_match and serializes_json:
                        self._claim(
                            text=(
                                f"{self.current_qualified_name} serializes JSON into SQLite "
                                f"table {insert_match.group(1)}."
                            ),
                            category="storage_serialization",
                            status="verified",
                            confidence=1.0,
                            importance="high",
                            supporting=(evidence.evidence_id,),
                        )

            if normalized.endswith(".add_middleware"):
                middleware = _expr_name(node.args[0]) if node.args else None
                origins = next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "allow_origins"),
                    None,
                )
                wildcard_origins = isinstance(origins, (ast.List, ast.Tuple, ast.Set)) and any(
                    _literal_string(item) == "*" for item in origins.elts
                )
                if (
                    middleware
                    and middleware.split(".")[-1] == "CORSMiddleware"
                    and wildcard_origins
                ):
                    self._claim(
                        text=(
                            f"{self.current_qualified_name} configures CORSMiddleware with a "
                            "wildcard allow_origins value."
                        ),
                        category="security_boundary",
                        status="verified",
                        confidence=1.0,
                        importance="critical",
                        supporting=(evidence.evidence_id,),
                    )

        self.generic_visit(node)

    def _attach_module_metadata(self) -> None:
        """Move facts gathered during the walk onto the module symbol.

        The module symbol is created before the tree is walked, so anything the
        walk discovers — tunable constants, observed value domains — has to be
        attached once the walk is done.
        """

        tunables = {
            name: {"value": value, "line": self.constant_lines.get(name, 1)}
            for name, value in sorted(self.numeric_constants.items())
        }
        tunables.update(sorted(self.instance_tunables.items()))
        payload: dict[str, Any] = {}
        if tunables:
            payload["tunables"] = tunables
        if self.state_fields:
            payload["state_fields"] = self.state_fields
        if self.data_containers:
            payload["data_containers"] = self.data_containers
        if self.payload_shapes:
            payload["payload_shapes"] = self.payload_shapes
        if self.model_fields:
            payload["model_fields"] = self.model_fields
        if self.imported_names:
            payload["imported_names"] = self.imported_names
        if self.string_constants:
            payload["string_constants"] = self.string_constants
        if self.embedded_literals:
            payload["embedded_literals"] = self.embedded_literals
        if self.signatures:
            payload["signatures"] = self.signatures
        if self.external_calls:
            payload["external_calls"] = self.external_calls
        if self.value_sets:
            payload["value_sets"] = self.value_sets
        if self.name_index:
            payload["name_index"] = self.name_index
        if self.cli_options or self.cli_positionals:
            # The readable claim names a bounded set so the sentence stays a
            # sentence. A command line is not partially useful, though -- a
            # reader wants the flag they are looking for, not the first twelve
            # alphabetically -- so the complete list travels here.
            payload["command_line"] = {
                "options": self.cli_options,
                "positionals": self.cli_positionals,
            }
        if not payload:
            return
        for index, symbol in enumerate(self.symbols):
            if symbol.kind == "module" and symbol.path == self.path:
                self.symbols[index] = replace(symbol, metadata={**symbol.metadata, **payload})
                return

    def finalize(self) -> None:
        self._attach_module_metadata()
        if (self.cli_options or self.cli_positionals) and describes_the_product(
            getattr(self.file_record, "role", None)
        ):
            parts: list[str] = []
            if self.cli_options:
                named = ", ".join(f"`{flag}`" for flag in self.cli_options[:12])
                more = (
                    f" and {len(self.cli_options) - 12:,} more"
                    if len(self.cli_options) > 12
                    else ""
                )
                parts.append(f"{len(self.cli_options):,} option(s): {named}{more}")
            if self.cli_positionals:
                named = ", ".join(f"`{name}`" for name in self.cli_positionals[:8])
                parts.append(f"{len(self.cli_positionals):,} positional argument(s): {named}")
            self._claim(
                text=(
                    f"{self.path} declares a command-line interface -- "
                    f"{'; '.join(parts)}. These are the words a user types; a "
                    "`__main__` guard says only that the module can be started."
                ),
                category="command_line_interface",
                status="verified",
                confidence=1.0,
                importance="high",
                supporting=(
                    self._evidence(
                        start_line=1,
                        end_line=max(1, getattr(self.file_record, "line_count", 1)),
                        symbol=self.module,
                        evidence_kind="command_line_interface",
                    ).evidence_id,
                ),
                invalidation_keys=(f"file:{self.path}",),
            )
        for name, base, line in self.defined_exceptions:
            qualified = f"{self.module}.{name}"
            self._claim(
                text=(
                    f"{qualified} extends {base}, so it is part of this package's "
                    "error contract: a caller catches it by name and renaming it "
                    "breaks them."
                ),
                category="exception_type",
                status="verified",
                confidence=1.0,
                importance="medium",
                supporting=(
                    self._evidence(
                        start_line=line,
                        end_line=line,
                        symbol=qualified,
                        evidence_kind="exception_type",
                    ).evidence_id,
                ),
                invalidation_keys=(f"file:{self.path}", f"symbol:{qualified}"),
            )
        for family, line in self.caught_families:
            self.caught_family_evidence.setdefault(family, []).append(
                self._evidence(
                    start_line=line,
                    end_line=line,
                    symbol=self.module,
                    evidence_kind="caught_exception",
                ).evidence_id
            )
        if self.test_evidence:
            self._claim(
                text=f"{self.path} declares {len(self.test_evidence)} test symbols.",
                category="testing",
                status="verified",
                confidence=1.0,
                importance="medium",
                supporting=tuple(sorted(set(self.test_evidence))),
            )
        if self.exit_evidence:
            codes = sorted({code for code, _ in self.exit_evidence if code is not None})
            rendered_codes = ", ".join(str(code) for code in codes) if codes else "dynamic"
            supporting = tuple(sorted({receipt for _, receipt in self.exit_evidence}))
            self._claim(
                text=(
                    f"{self.path} contains explicit process termination calls with observed "
                    f"exit codes: {rendered_codes}."
                ),
                category="process_termination",
                status="verified",
                confidence=1.0,
                importance="medium",
                supporting=supporting,
            )
            if self.path.startswith("scripts/"):
                self._claim(
                    text=(
                        f"{self.path} is an operator-harness candidate rather than an "
                        "automatically discovered unit test."
                    ),
                    category="operator_harness",
                    status="inferred",
                    confidence=0.9,
                    importance="medium",
                    supporting=supporting,
                    invalidation_keys=(f"file:{self.path}",),
                    alternatives=("The script may be an operational CLI with no testing purpose.",),
                )


class PythonAstAnalyzer:
    name = ANALYZER_NAME
    version = ANALYZER_VERSION
    eligibility = "language"

    def analyze(self, snapshot: Snapshot) -> AnalysisResult:
        started = time.perf_counter()
        created_at = utc_now()
        symbols: list[SymbolRecord] = []
        edges: list[EdgeRecord] = []
        evidence: list[EvidenceRecord] = []
        claims: list[ClaimRecord] = []
        failures: list[str] = []
        route_evidence: list[str] = []
        endpoint_evidence: list[str] = []
        endpoint_literals: set[str] = set()
        caught_families: dict[str, list[str]] = defaultdict(list)
        route_auth_control_evidence: list[str] = []
        typed_route_evidence: list[str] = []
        eligible = [item for item in snapshot.files if item.language == "Python"]
        analyzed_files = 0
        # Package roots come from every path in the snapshot, not just the
        # readable ones: an `__init__.py` that fails to parse still marks its
        # directory as a package, and losing that would rename its siblings.
        packages = _package_directories(item.path for item in snapshot.files)
        module_names = _module_names((item.path for item in eligible), packages)

        for file_record in eligible:
            source_path = snapshot.root / Path(file_record.path)
            try:
                payload = source_path.read_bytes()
                current_hash = hashlib.sha256(payload).hexdigest()
                if current_hash != file_record.sha256:
                    raise ValueError("content changed after snapshot")
                source = payload.decode("utf-8", errors="strict")
                tree = ast.parse(source, filename=file_record.path, type_comments=True)
                analyzer = _PythonFileAnalyzer(
                    snapshot=snapshot,
                    file_record=file_record,
                    source=source,
                    tree=tree,
                    created_at=created_at,
                    module=module_names[file_record.path],
                    local_modules=frozenset(module_names.values()),
                )
                analyzer.visit(tree)
                analyzer.finalize()
            # Walking the tree is inside this guard, not only parsing it. One
            # file in sympy is a single arithmetic expression nested 401 nodes
            # deep, and `ast.NodeVisitor` recurses per node: it raised
            # `RecursionError` from the walk, which no handler covered, and a
            # 2,600-file repository produced nothing at all. A file this
            # analyzer cannot finish is a file it did not read, which is a
            # coverage failure it already knows how to report.
            except (
                OSError,
                UnicodeDecodeError,
                SyntaxError,
                ValueError,
                RecursionError,
            ) as exc:
                failures.append(f"{file_record.path}: {exc.__class__.__name__}: {exc}")
                continue

            # Nothing partial reaches the result: a file contributes every
            # record it produced or none of them.
            symbols.extend(analyzer.symbols)
            edges.extend(analyzer.edges)
            evidence.extend(analyzer.evidence)
            claims.extend(analyzer.claims)
            route_evidence.extend(analyzer.route_evidence)
            endpoint_evidence.extend(analyzer.endpoint_evidence)
            endpoint_literals.update(analyzer.endpoint_literals)
            # The per-claim choke point re-files a test file's claims by
            # category, and cannot reach this one: the error contract is
            # aggregated across files and emitted once at the end, so a
            # handler inside a suite entered the program's contract without
            # passing the check that exists to stop exactly that. Skipping
            # test-role files here is the same rule applied where the claim
            # is actually built.
            if str(file_record.role) != "test":
                for family, receipts in analyzer.caught_family_evidence.items():
                    caught_families[family].extend(receipts)
            route_auth_control_evidence.extend(analyzer.route_auth_control_evidence)
            typed_route_evidence.extend(analyzer.typed_route_evidence)
            analyzed_files += 1

        for family, receipts in sorted(caught_families.items()):
            rendered = "every exception" if family == "*" else f"`{family}`"
            claims.append(
                ClaimRecord(
                    claim_id=stable_id(
                        "claim",
                        (snapshot.snapshot_id, "caught_exception", family, ANALYZER_VERSION),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    claim=(
                        f"{len(receipts):,} handler(s) catch {rendered}. What a program "
                        "chooses to absorb is where it decided a fault is survivable."
                    ),
                    category="caught_exception",
                    status="verified",
                    confidence=1.0,
                    importance="high" if family == "*" else "medium",
                    produced_by=ANALYZER_VERSION,
                    created_at=created_at,
                    verified_at=created_at,
                    supporting_evidence=tuple(sorted(receipts)),
                    invalidation_keys=("python:exception-handling",),
                )
            )

        if endpoint_evidence:
            listed = ", ".join(sorted(endpoint_literals))
            endpoint_text = (
                f"Python source hardcodes {len(endpoint_literals)} distinct network "
                f"endpoint(s) across {len(endpoint_evidence)} site(s): {listed}. No "
                "environment lookup supplies them at those sites."
            )
            claims.append(
                ClaimRecord(
                    claim_id=stable_id(
                        "claim",
                        (
                            snapshot.snapshot_id,
                            "hardcoded_endpoint",
                            endpoint_text,
                            ANALYZER_VERSION,
                        ),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    claim=endpoint_text,
                    category="hardcoded_endpoint",
                    status="verified",
                    confidence=1.0,
                    importance="high",
                    produced_by=ANALYZER_VERSION,
                    created_at=created_at,
                    verified_at=created_at,
                    supporting_evidence=tuple(sorted(set(endpoint_evidence))),
                    invalidation_keys=("language:python",),
                )
            )

        if route_evidence:
            text = f"Python source declares {len(route_evidence)} HTTP route handlers."
            claims.append(
                ClaimRecord(
                    claim_id=stable_id(
                        "claim",
                        (snapshot.snapshot_id, "http_route_inventory", text, ANALYZER_VERSION),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    claim=text,
                    category="http_route_inventory",
                    status="verified",
                    confidence=1.0,
                    importance="high",
                    produced_by=ANALYZER_VERSION,
                    created_at=created_at,
                    verified_at=created_at,
                    supporting_evidence=tuple(sorted(set(route_evidence))),
                    invalidation_keys=tuple(sorted({f"file:{item.path}" for item in eligible})),
                )
            )

            if typed_route_evidence:
                validation_text = (
                    f"{len(typed_route_evidence)} detected FastAPI route handlers declare typed "
                    "path or query parameters; FastAPI framework validation may return HTTP 422 "
                    "before handler execution."
                )
                claims.append(
                    ClaimRecord(
                        claim_id=stable_id(
                            "claim",
                            (
                                snapshot.snapshot_id,
                                "http_framework_behavior",
                                validation_text,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        claim=validation_text,
                        category="http_framework_behavior",
                        status="inferred",
                        confidence=0.95,
                        importance="high",
                        produced_by=ANALYZER_VERSION,
                        created_at=created_at,
                        supporting_evidence=tuple(sorted(set(typed_route_evidence))),
                        invalidation_keys=tuple(sorted({f"file:{item.path}" for item in eligible})),
                        alternative_hypotheses=(
                            (
                                "Application or deployment middleware may transform framework-generated "
                                "validation responses."
                            ),
                        ),
                    )
                )
            if not route_auth_control_evidence:
                auth_text = (
                    f"None of the {len(route_evidence)} detected Python route declarations uses "
                    "Depends or Security in its signature; this census does not rule out "
                    "custom checks inside handler bodies or middleware."
                )
                claims.append(
                    ClaimRecord(
                        claim_id=stable_id(
                            "claim",
                            (
                                snapshot.snapshot_id,
                                "auth_control_census",
                                auth_text,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        claim=auth_text,
                        category="auth_control_census",
                        status="verified",
                        confidence=1.0,
                        importance="high",
                        produced_by=ANALYZER_VERSION,
                        created_at=created_at,
                        verified_at=created_at,
                        supporting_evidence=tuple(sorted(set(route_evidence))),
                        invalidation_keys=tuple(sorted({f"file:{item.path}" for item in eligible})),
                        alternative_hypotheses=(
                            (
                                "Authentication or authorization may be implemented inside handlers, "
                                "middleware, a proxy, or an external network boundary."
                            ),
                        ),
                    )
                )
            else:
                control_text = (
                    f"{len(route_auth_control_evidence)} of the {len(route_evidence)} "
                    "detected Python route declarations declare a Depends or Security "
                    "dependency; the census does not evaluate what those dependencies "
                    "enforce."
                )
                claims.append(
                    ClaimRecord(
                        claim_id=stable_id(
                            "claim",
                            (
                                snapshot.snapshot_id,
                                "auth_control",
                                control_text,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        claim=control_text,
                        category="auth_control",
                        status="verified",
                        confidence=1.0,
                        importance="high",
                        produced_by=ANALYZER_VERSION,
                        created_at=created_at,
                        verified_at=created_at,
                        supporting_evidence=tuple(sorted(set(route_auth_control_evidence))),
                        invalidation_keys=tuple(sorted({f"file:{item.path}" for item in eligible})),
                        alternative_hypotheses=(
                            (
                                "A declared dependency may perform validation, rate limiting, or "
                                "another concern rather than authentication."
                            ),
                        ),
                    )
                )

        coverage = CoverageRecord(
            analyzer=ANALYZER_VERSION,
            language="Python",
            eligible_files=len(eligible),
            analyzed_files=analyzed_files,
            failed_files=len(failures),
            unsupported_files=0,
            failures=tuple(failures),
        )
        duration_ms = round((time.perf_counter() - started) * 1000)
        return AnalysisResult(
            snapshot_id=snapshot.snapshot_id,
            analyzer_version=ANALYZER_VERSION,
            created_at=created_at,
            duration_ms=duration_ms,
            symbols=tuple(
                sorted(symbols, key=lambda item: (item.path, item.start_line, item.qualified_name))
            ),
            edges=tuple(
                sorted(
                    edges, key=lambda item: (item.source_path, item.relationship, item.target_ref)
                )
            ),
            evidence=tuple(
                sorted(
                    evidence, key=lambda item: (item.path, item.start_line or 0, item.evidence_id)
                )
            ),
            claims=tuple(sorted(claims, key=lambda item: (item.category, item.claim))),
            coverage=(coverage,),
        )
