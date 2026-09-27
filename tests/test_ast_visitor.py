# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import ast
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.ast_visitor import FastNodeVisitor, iter_child_nodes, walk
from open_skeleton.scanner import scan_repository

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "open_skeleton"

# Every construct whose fields are unusual: optional children (`returns`,
# `orelse`), lists holding `None` (`dict` keys under `**`), nested
# comprehensions, match statements, type parameters and f-strings.
AWKWARD_SOURCE = """\
from __future__ import annotations
import os as system, sys
from . import sibling
@decorator(1, key=2)
class Shape[T](Base, metaclass=Meta):
    field: int = 0
    async def area(self, *args: T, scale=1.0, **kwargs) -> float:
        async with guard() as held, other():
            async for item in stream():
                yield item
        return {**kwargs, "a": [x for x in args if x for y in x], "b": lambda q=1: q}
def matcher(value):
    match value:
        case {"kind": "circle", **rest} if rest:
            return f"{value!r:>{width}} {rest}"
        case [first, *_] | (first,):
            return first
        case Point(x=0) as point:
            return point
        case _:
            pass
    try:
        del value[1:2, ...]
    except* (TypeError, ValueError) as grouped:
        raise RuntimeError from grouped
    finally:
        global counter
    while (walrus := next(it, None)) is not None:
        counter += ~walrus if not walrus else -walrus
    else:
        assert counter, "never"
type Alias = list[int]
"""


class _Recorder(ast.NodeVisitor):
    """Records every node it enters, and intervenes the way the readers do."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def _note(self, node: ast.AST) -> None:
        self.seen.append(f"{type(node).__name__}:{getattr(node, 'lineno', '-')}")

    def generic_visit(self, node: ast.AST) -> None:
        self._note(node)
        super().generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # A handler that stops at the body, as the scope-bounded readers do.
        self.seen.append(f"def:{node.name}")
        for decorator in node.decorator_list:
            self.visit(decorator)

    def visit_Call(self, node: ast.Call) -> None:
        self.seen.append("call")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        self.seen.append(f"constant:{node.value!r}")


class _FastRecorder(FastNodeVisitor):
    def __init__(self) -> None:
        self.seen: list[str] = []

    def _note(self, node: ast.AST) -> None:
        self.seen.append(f"{type(node).__name__}:{getattr(node, 'lineno', '-')}")

    def generic_visit(self, node: ast.AST) -> None:
        self._note(node)
        super().generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.seen.append(f"def:{node.name}")
        for decorator in node.decorator_list:
            self.visit(decorator)

    def visit_Call(self, node: ast.Call) -> None:
        self.seen.append("call")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        self.seen.append(f"constant:{node.value!r}")


class _PlainStandard(ast.NodeVisitor):
    def __init__(self) -> None:
        self.seen: list[str] = []

    def generic_visit(self, node: ast.AST) -> None:
        self.seen.append(type(node).__name__)
        super().generic_visit(node)


class _PlainFast(FastNodeVisitor):
    def __init__(self) -> None:
        self.seen: list[str] = []

    def generic_visit(self, node: ast.AST) -> None:
        self.seen.append(type(node).__name__)
        super().generic_visit(node)


def _trees() -> list[tuple[str, ast.AST]]:
    trees: list[tuple[str, ast.AST]] = [("awkward", ast.parse(AWKWARD_SOURCE))]
    trees.extend(
        (str(path.relative_to(SOURCE_ROOT)), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
    )
    return trees


class FastWalkTests(TestCase):
    def test_walk_yields_the_standard_nodes_in_the_standard_order(self) -> None:
        # Identity, not equality: the same node objects in the same sequence,
        # over a file built from unusual syntax and over this engine's own
        # sources, which is several hundred thousand nodes of real code.
        for label, tree in _trees():
            with self.subTest(tree=label):
                self.assertEqual(
                    [id(node) for node in walk(tree)], [id(node) for node in ast.walk(tree)]
                )

    def test_child_nodes_match_the_standard_children(self) -> None:
        for label, tree in _trees():
            with self.subTest(tree=label):
                for node in ast.walk(tree):
                    self.assertEqual(
                        [id(child) for child in iter_child_nodes(node)],
                        [id(child) for child in ast.iter_child_nodes(node)],
                    )

    def test_a_node_missing_an_optional_field_is_walked_not_rejected(self) -> None:
        # Nodes built by hand, as a transformer would, may omit fields the
        # parser always sets. The standard walk skips what is absent.
        node = ast.BinOp(left=ast.Name(id="a"), op=ast.Add())  # type: ignore[call-arg]
        self.assertFalse(hasattr(node, "right"))
        self.assertEqual([id(item) for item in walk(node)], [id(item) for item in ast.walk(node)])
        self.assertEqual(
            [id(item) for item in iter_child_nodes(node)],
            [id(item) for item in ast.iter_child_nodes(node)],
        )


class FastNodeVisitorTests(TestCase):
    def test_dispatch_and_order_match_the_standard_visitor(self) -> None:
        for label, tree in _trees():
            with self.subTest(tree=label):
                standard, fast = _Recorder(), _FastRecorder()
                standard.visit(tree)
                fast.visit(tree)
                self.assertEqual(fast.seen, standard.seen)

    def test_constants_fall_to_generic_visit_without_a_handler(self) -> None:
        # The standard visitor's own `visit_Constant` exists only to reach the
        # removed `visit_Num` family; without a handler it is generic_visit.
        tree = ast.parse("x = 1 + 'two'")
        standard, fast = _PlainStandard(), _PlainFast()
        standard.visit(tree)
        fast.visit(tree)
        self.assertEqual(fast.seen, standard.seen)
        self.assertIn("Constant", fast.seen)

    def test_a_handler_the_fast_visitor_would_never_call_is_refused(self) -> None:
        # Silently skipping a handler is the one way this visitor could
        # differ from the standard one, so defining it fails immediately.
        with self.assertRaises(TypeError):

            class _Legacy(FastNodeVisitor):
                def visit_Num(self, node: ast.AST) -> None:
                    pass

    def test_each_subclass_resolves_its_own_handlers(self) -> None:
        class _Names(FastNodeVisitor):
            def __init__(self) -> None:
                self.names: list[str] = []

            def visit_Name(self, node: ast.Name) -> None:
                self.names.append(node.id)

        class _Nothing(FastNodeVisitor):
            pass

        tree = ast.parse("a = b")
        _Nothing().visit(tree)
        names = _Names()
        names.visit(tree)
        self.assertEqual(names.names, ["a", "b"])

    def test_a_pathologically_deep_file_is_a_coverage_failure_not_a_crash(self) -> None:
        # The recursion is two frames per level, as it was with the standard
        # visitor, so the reader's guard for RecursionError still decides.
        depth = sys.getrecursionlimit()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deep.py").write_text(
                "value = " + "-" * depth + "1\n" + "ok = 1\n", encoding="utf-8"
            )
            (root / "fine.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
        [python] = [item for item in result.coverage if item.language == "Python"]
        self.assertEqual(python.analyzed_files, 1)
        self.assertEqual(python.failed_files, 1)
        self.assertTrue(python.failures[0].startswith("deep.py: "))
