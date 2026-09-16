# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Ownership facts, which are only worth having if they are exact.

Work package 2 sets one condition on every topology statement: a path of edge
and evidence IDs, and an ambiguous target left unresolved. So each test here
pins a counting rule rather than a judgement, and the two that matter most are
the refusals -- an unresolved edge says nothing, and a directory with two doors
has none.
"""

from __future__ import annotations

from unittest import TestCase

from open_skeleton.models import EdgeRecord, FileRecord, SymbolRecord
from open_skeleton.topology import PackageDoor, Topology, describe, facades


def _file(path: str) -> FileRecord:
    return FileRecord(
        path=path,
        size_bytes=1,
        sha256="0" * 64,
        language="Python",
        line_count=1,
        role="source",
    )


def _symbol(path: str) -> SymbolRecord:
    return SymbolRecord(
        symbol_id=f"symbol:{path}",
        snapshot_id="snapshot",
        path=path,
        qualified_name=path.replace("/", ".").removesuffix(".py"),
        kind="module",
        start_line=1,
        end_line=1,
        language="Python",
        analyzer="test",
        metadata={},
    )


def _edge(source: str, target: str | None, *, evidence: str = "evidence:one") -> EdgeRecord:
    return EdgeRecord(
        edge_id=f"edge:{source}->{target}",
        snapshot_id="snapshot",
        source_symbol_id=None,
        source_path=source,
        relationship="imports",
        target_ref=str(target),
        target_symbol_id=f"symbol:{target}" if target else None,
        evidence_id=evidence,
        analyzer="test",
    )


class FacadeTests(TestCase):
    def test_a_package_file_is_its_directory_s_door(self) -> None:
        found = facades([_file("pkg/__init__.py"), _file("pkg/inner.py")])
        self.assertEqual(found, {"pkg": "pkg/__init__.py"})

    def test_a_directory_without_one_has_no_door(self) -> None:
        self.assertEqual(facades([_file("pkg/inner.py")]), {})

    def test_two_doors_are_no_door(self) -> None:
        # A Rust crate holding both `lib.rs` and `main.rs` has two entry
        # points, and preferring one would be the adjudication the resolver
        # refuses elsewhere for the same reason.
        found = facades([_file("crate/src/lib.rs"), _file("crate/src/main.rs")])
        self.assertEqual(found, {})

    def test_a_root_level_file_belongs_to_no_package(self) -> None:
        self.assertEqual(facades([_file("__init__.py")]), {})


class OwnershipTests(TestCase):
    FILES = [
        _file("pkg/__init__.py"),
        _file("pkg/inner.py"),
        _file("pkg/other.py"),
        _file("app.py"),
        _file("tool.py"),
    ]
    SYMBOLS = [_symbol(item.path) for item in FILES]

    def _describe(self, *edges: EdgeRecord) -> Topology:
        return describe(self.FILES, self.SYMBOLS, list(edges))

    def test_a_module_with_one_importer_is_owned_by_it(self) -> None:
        topology = self._describe(_edge("app.py", "pkg/inner.py"))
        self.assertEqual(topology.sole_owned, {"pkg/inner.py": "app.py"})
        self.assertEqual(topology.sole_evidence["pkg/inner.py"], "evidence:one")

    def test_a_module_with_two_importers_is_owned_by_neither(self) -> None:
        topology = self._describe(
            _edge("app.py", "pkg/inner.py"),
            _edge("tool.py", "pkg/inner.py"),
        )
        self.assertEqual(topology.sole_owned, {})

    def test_the_same_importer_twice_is_still_one_importer(self) -> None:
        # Two `from x import a` and `from x import b` lines are two edges and
        # one caller.
        topology = self._describe(
            _edge("app.py", "pkg/inner.py", evidence="evidence:a"),
            _edge("app.py", "pkg/inner.py", evidence="evidence:b"),
        )
        self.assertEqual(topology.sole_owned, {"pkg/inner.py": "app.py"})

    def test_an_unresolved_edge_says_nothing(self) -> None:
        # Counting it would turn the shape of the resolver into a claim about
        # the repository.
        topology = self._describe(_edge("app.py", None))
        self.assertEqual(topology.sole_owned, {})
        self.assertEqual(topology.resolved_edges, 0)

    def test_a_file_importing_itself_is_not_an_owner(self) -> None:
        topology = self._describe(_edge("pkg/inner.py", "pkg/inner.py"))
        self.assertEqual(topology.sole_owned, {})


class PackageDoorTests(TestCase):
    FILES = OwnershipTests.FILES
    SYMBOLS = OwnershipTests.SYMBOLS

    def _doors(self, *edges: EdgeRecord) -> dict[str, PackageDoor]:
        return {
            door.package: door for door in describe(self.FILES, self.SYMBOLS, list(edges)).doors
        }

    def test_naming_the_facade_from_outside_goes_through_it(self) -> None:
        doors = self._doors(_edge("app.py", "pkg/__init__.py"))
        self.assertEqual((doors["pkg"].through, doors["pkg"].around), (1, 0))
        self.assertEqual(doors["pkg"].through_evidence, ("evidence:one",))

    def test_naming_a_module_inside_from_outside_goes_around_it(self) -> None:
        doors = self._doors(_edge("app.py", "pkg/inner.py"))
        self.assertEqual((doors["pkg"].through, doors["pkg"].around), (0, 1))

    def test_an_import_from_inside_the_package_is_neither(self) -> None:
        # Reaching a sibling is how a package is written, not a way around
        # anything, so it is not counted as either.
        self.assertEqual(self._doors(_edge("pkg/other.py", "pkg/inner.py")), {})

    def test_the_facade_reaching_its_own_module_is_neither(self) -> None:
        self.assertEqual(self._doors(_edge("pkg/__init__.py", "pkg/inner.py")), {})

    def test_both_ways_are_counted_separately(self) -> None:
        doors = self._doors(
            _edge("app.py", "pkg/__init__.py", evidence="evidence:a"),
            _edge("tool.py", "pkg/inner.py", evidence="evidence:b"),
            _edge("tool.py", "pkg/other.py", evidence="evidence:c"),
        )
        self.assertEqual((doors["pkg"].through, doors["pkg"].around), (1, 2))
        self.assertEqual(doors["pkg"].facade, "pkg/__init__.py")
