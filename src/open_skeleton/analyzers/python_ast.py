# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import ast
import hashlib
import re
import time
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

ANALYZER_NAME = "python-ast"
ANALYZER_VERSION = "python-ast/v2"
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "options", "head"})
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
INSERT_TABLE_PATTERN = re.compile(
    r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+[\"`\[]?([A-Za-z_][\w]*)",
    re.IGNORECASE,
)


def _module_name(path: str) -> str:
    parts = path.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "__root__"


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


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
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
                    events.append(
                        {
                            "kind": "raise",
                            "line": statement.lineno,
                            "label": summary,
                            "depth": depth,
                        }
                    )
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
        if len(entry["values"]) >= MIN_STATE_VALUES and entry["entries"]
    }


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
            self.mutations[candidate.id].append(evidence_node)

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
    ) -> None:
        self.snapshot = snapshot
        self.file_record = file_record
        self.path = file_record.path
        self.module = _module_name(file_record.path)
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
        self.test_evidence: list[str] = []
        self.route_evidence: list[str] = []
        self.route_path_literals: set[str] = set()
        self.route_auth_control_evidence: list[str] = []
        self.typed_route_evidence: list[str] = []
        self.exit_evidence: list[tuple[int | None, str]] = []
        self.numeric_constants: dict[str, float] = {}
        mutation_collector = _ModuleMutationCollector(_module_mutable_names(tree))
        mutation_collector.visit(tree)
        self.module_mutations = mutation_collector.mutations

        module_end = max(1, file_record.line_count)
        state_fields = _state_fields(tree)
        module_symbol = self._symbol(
            qualified_name=self.module,
            kind="module",
            start_line=1,
            end_line=module_end,
            metadata=({"state_fields": state_fields} if state_fields else {}),
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
                }
            )
        return routes

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
        metadata: dict[str, Any] = {}
        if routes:
            metadata["routes"] = routes
            metadata["control_flow"] = _control_flow(node)
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
            if typed_parameters:
                signature_evidence = self._evidence(
                    start_line=node.lineno,
                    end_line=node.lineno,
                    symbol=qualified,
                    evidence_kind="typed_route_signature",
                )
                self.typed_route_evidence.append(signature_evidence.evidence_id)
            self._claim(
                text=f"{route['method']} {route['path']} is handled by {qualified}.",
                category="http_route",
                status="verified",
                confidence=1.0,
                importance="medium",
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
        self.generic_visit(node)
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
            if call_name and call_name.split(".")[-1] == "FastAPI":
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

    def visit_Assign(self, node: ast.Assign) -> None:
        self._module_assignment(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._module_assignment(node)
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        if len(self.scope_names) == 1 and _is_main_guard(node):
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

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Pow):
            base = _literal_number(node.left)
            if base is None and isinstance(node.left, ast.Name):
                base = self.numeric_constants.get(node.left.id)
            exponent = ast.unparse(node.right)
            if base is not None and "ascension" in exponent.casefold():
                evidence = self._evidence(
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    symbol=self.current_qualified_name,
                    evidence_kind="exponential_scaling",
                )
                self._claim(
                    text=(
                        f"{self.current_qualified_name} exponentiates base {base:g} by "
                        f"ascension-related expression `{exponent}`."
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
        has_ai_call = any(
            "ai_client" in name.casefold()
            or name.casefold().endswith(("generate_content", "generate_json", "stream_content"))
            for name in call_names
        )
        fallback_labels: set[str] = set()
        if has_ai_call:
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
            self._claim(
                text=(
                    f"{self.current_qualified_name} has an exception handler around AI-client "
                    f"calls whose explicit fallback value is {rendered}."
                ),
                category="ai_failure_behavior",
                status="verified",
                confidence=1.0,
                importance="high",
                supporting=(evidence.evidence_id,),
            )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
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
                if sql:
                    for match in CREATE_TABLE_PATTERN.finditer(sql):
                        table = match.group(1)
                        self._claim(
                            text=f"{self.current_qualified_name} creates SQLite table {table}.",
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

    def finalize(self) -> None:
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

    def analyze(self, snapshot: Snapshot) -> AnalysisResult:
        started = time.perf_counter()
        created_at = utc_now()
        symbols: list[SymbolRecord] = []
        edges: list[EdgeRecord] = []
        evidence: list[EvidenceRecord] = []
        claims: list[ClaimRecord] = []
        failures: list[str] = []
        route_evidence: list[str] = []
        route_auth_control_evidence: list[str] = []
        typed_route_evidence: list[str] = []
        eligible = [item for item in snapshot.files if item.language == "Python"]
        analyzed_files = 0

        for file_record in eligible:
            source_path = snapshot.root / Path(file_record.path)
            try:
                payload = source_path.read_bytes()
                current_hash = hashlib.sha256(payload).hexdigest()
                if current_hash != file_record.sha256:
                    raise ValueError("content changed after snapshot")
                source = payload.decode("utf-8", errors="strict")
                tree = ast.parse(source, filename=file_record.path, type_comments=True)
            except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
                failures.append(f"{file_record.path}: {exc.__class__.__name__}: {exc}")
                continue

            analyzer = _PythonFileAnalyzer(
                snapshot=snapshot,
                file_record=file_record,
                source=source,
                tree=tree,
                created_at=created_at,
            )
            analyzer.visit(tree)
            analyzer.finalize()
            symbols.extend(analyzer.symbols)
            edges.extend(analyzer.edges)
            evidence.extend(analyzer.evidence)
            claims.extend(analyzer.claims)
            route_evidence.extend(analyzer.route_evidence)
            route_auth_control_evidence.extend(analyzer.route_auth_control_evidence)
            typed_route_evidence.extend(analyzer.typed_route_evidence)
            analyzed_files += 1

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
                    f"None of the {len(route_evidence)} detected FastAPI route signatures uses "
                    "Depends or Security; this census does not rule out custom checks inside "
                    "handler bodies or middleware."
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
                    "detected FastAPI route signatures declare a Depends or Security "
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
