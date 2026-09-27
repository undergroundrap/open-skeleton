# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""A drop-in `ast.NodeVisitor` that finds its handler once per node type.

`ast.NodeVisitor.visit` builds the string `"visit_" + class name` and looks it
up on the instance for every node it enters, and `generic_visit` reaches the
children through two generator layers, `iter_fields` and a per-field
`isinstance` pair. On this engine's own sources that bookkeeping was a third of
the time the Python reader spent on a file, and none of it depends on the node:
the handler for `ast.Name` is the same function for every `Name` in a run.

The traversal is the standard one, unchanged, so a subclass sees the same nodes
in the same order and may call `generic_visit` or `visit` exactly as before:

- `visit` dispatches to `visit_<ClassName>` when the class defines one, and to
  `generic_visit` otherwise.
- `generic_visit` visits each field in `_fields` order, a list field item by
  item, skipping anything that is not a node and any field the node lacks.
- The recursion is still `visit` -> `generic_visit` -> `visit`, two frames per
  level, so a pathologically deep tree raises `RecursionError` where the
  standard visitor would, and the caller's guard for it still applies.

The one behaviour deliberately not carried over is the standard visitor's
fallback from `visit_Constant` to the `visit_Num`/`visit_Str` family removed in
Python 3.14. No reader in this engine defines those, and `test_ast_visitor`
fails if one ever does rather than letting it be silently skipped.
"""

from __future__ import annotations

import ast
from collections import deque
from collections.abc import Callable, Iterator
from typing import Any

_DEPRECATED_CONSTANT_VISITORS = (
    "visit_Num",
    "visit_Str",
    "visit_Bytes",
    "visit_NameConstant",
    "visit_Ellipsis",
)


class FastNodeVisitor(ast.NodeVisitor):
    """`ast.NodeVisitor` with per-type dispatch resolved once per class."""

    # Filled lazily per subclass: node type -> unbound handler, or None where
    # the subclass has no `visit_<Type>` and the node falls to `generic_visit`.
    handlers_by_type: dict[type, Callable[..., Any] | None]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.handlers_by_type = {}
        for name in _DEPRECATED_CONSTANT_VISITORS:
            if hasattr(cls, name):
                raise TypeError(
                    f"{cls.__name__}.{name} would never run: FastNodeVisitor dispatches "
                    "Constant nodes to visit_Constant only"
                )

    def visit(self, node: ast.AST) -> Any:
        node_type = type(node)
        dispatch = type(self).handlers_by_type
        try:
            handler = dispatch[node_type]
        except KeyError:
            handler = getattr(type(self), "visit_" + node_type.__name__, None)
            # The standard visitor's own `visit_Constant` only exists to reach
            # the deprecated handlers; with none defined it is `generic_visit`.
            if handler is ast.NodeVisitor.visit_Constant:
                handler = None
            dispatch[node_type] = handler
        if handler is None:
            return self.generic_visit(node)
        return handler(self, node)

    def generic_visit(self, node: ast.AST) -> None:
        for field in node._fields:
            try:
                value = getattr(node, field)
            except AttributeError:
                continue
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        self.visit(item)
            elif isinstance(value, ast.AST):
                self.visit(value)


def iter_child_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """`ast.iter_child_nodes` without the `iter_fields` generator beneath it."""

    for field in node._fields:
        try:
            value = getattr(node, field)
        except AttributeError:
            continue
        if isinstance(value, ast.AST):
            yield value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, ast.AST):
                    yield item


def walk(node: ast.AST) -> Iterator[ast.AST]:
    """`ast.walk`, in the same breadth-first order, at roughly half the cost.

    The standard walk extends its queue from `iter_child_nodes`, which is
    itself a generator over `iter_fields`: two generator frames per node
    before any node is yielded. Readers here walk many subtrees of each file,
    so that overhead was paid millions of times per run on a large codebase.
    """

    pending = deque([node])
    pop = pending.popleft
    push = pending.append
    node_base = ast.AST
    while pending:
        current = pop()
        for field in current._fields:
            try:
                value = getattr(current, field)
            except AttributeError:
                continue
            if isinstance(value, node_base):
                push(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, node_base):
                        push(item)
        yield current
