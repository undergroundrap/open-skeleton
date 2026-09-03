# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""An import edge names a symbol, or it names nothing.

The graph resolved 2,857 of 20,481 edges for this repository and every one of
them was a `contains` edge. Work package 2 wants owners and fan-in proved by a
path of edge IDs, and a name with no target is not a path.
"""

from __future__ import annotations

from collections.abc import Sequence
from unittest import TestCase

from open_skeleton.models import EdgeRecord, FileRecord, SymbolRecord
from open_skeleton.resolution import resolve_import_targets


def _file(path: str, language: str = "Rust") -> FileRecord:
    return FileRecord(
        path=path, sha256="0" * 64, size_bytes=1, line_count=1, language=language, role="source"
    )


def _symbol(symbol_id: str, path: str, qualified_name: str, language: str = "Rust") -> SymbolRecord:
    return SymbolRecord(
        symbol_id=symbol_id,
        snapshot_id="s",
        path=path,
        qualified_name=qualified_name,
        kind="module",
        start_line=1,
        end_line=1,
        language=language,
        analyzer="test",
    )


def _edge(source_path: str, target_ref: str, relationship: str = "imports") -> EdgeRecord:
    return EdgeRecord(
        edge_id=f"{source_path}->{target_ref}",
        snapshot_id="s",
        source_symbol_id=None,
        source_path=source_path,
        relationship=relationship,
        target_ref=target_ref,
        target_symbol_id=None,
        evidence_id=None,
        analyzer="test",
    )


class ImportResolutionTests(TestCase):
    def _resolve(
        self,
        files: Sequence[FileRecord],
        symbols: Sequence[SymbolRecord],
        edges: Sequence[EdgeRecord],
    ) -> dict[str, str | None]:
        return {
            item.edge_id: item.target_symbol_id
            for item in resolve_import_targets(tuple(files), tuple(symbols), tuple(edges))
        }

    def test_an_absolute_dotted_reference_names_its_symbol(self) -> None:
        # What Python already emits: the reference is the qualified name.
        files = [_file("app/main.py", "Python"), _file("app/store.py", "Python")]
        symbols = [_symbol("s1", "app/store.py", "app.store.Ledger", "Python")]
        edges = [_edge("app/main.py", "app.store.Ledger")]
        self.assertEqual(
            self._resolve(files, symbols, edges)["app/main.py->app.store.Ledger"], "s1"
        )

    def test_a_reference_to_code_this_repository_lacks_stays_unresolved(self) -> None:
        # `pathlib` and `serde` are real references to real code that is not
        # here. Leaving them empty is the true answer, not a gap.
        files = [_file("app/main.py", "Python")]
        edges = [_edge("app/main.py", "pathlib.Path")]
        self.assertIsNone(self._resolve(files, [], edges)["app/main.py->pathlib.Path"])

    def test_a_name_belonging_to_two_symbols_is_not_adjudicated(self) -> None:
        files = [_file("app/main.py", "Python")]
        symbols = [
            _symbol("s1", "one/util.py", "util.helper", "Python"),
            _symbol("s2", "two/util.py", "util.helper", "Python"),
        ]
        edges = [_edge("app/main.py", "util.helper")]
        self.assertIsNone(self._resolve(files, symbols, edges)["app/main.py->util.helper"])

    def test_a_relative_reference_is_read_from_the_file_holding_it(self) -> None:
        # TypeScript writes `./skillCards`, which matched nothing at all
        # until it was resolved against its own directory.
        files = [_file("src/data/index.ts", "TypeScript")]
        symbols = [_symbol("s1", "src/data/skillCards.ts", "src.data.skillCards", "TypeScript")]
        edges = [_edge("src/data/index.ts", "./skillCards")]
        self.assertEqual(
            self._resolve(files, symbols, edges)["src/data/index.ts->./skillCards"], "s1"
        )

    def test_a_parent_relative_reference_climbs_before_it_matches(self) -> None:
        files = [_file("src/app/page.tsx", "TypeScript JSX")]
        symbols = [
            _symbol("s1", "src/components/Card.tsx", "src.components.Card", "TypeScript JSX")
        ]
        edges = [_edge("src/app/page.tsx", "../components/Card")]
        found = self._resolve(files, symbols, edges)
        self.assertEqual(found["src/app/page.tsx->../components/Card"], "s1")

    def test_a_directory_reference_finds_the_module_inside_it(self) -> None:
        files = [_file("src/app/page.tsx", "TypeScript JSX")]
        symbols = [_symbol("s1", "src/data/index.ts", "src.data.index", "TypeScript")]
        edges = [_edge("src/app/page.tsx", "../data")]
        self.assertEqual(self._resolve(files, symbols, edges)["src/app/page.tsx->../data"], "s1")

    def test_a_crate_reference_drops_its_prefix(self) -> None:
        files = [_file("crates/core/Cargo.toml"), _file("crates/core/src/lib.rs")]
        symbols = [_symbol("s1", "crates/core/src/units.rs", "units::Watts")]
        edges = [_edge("crates/core/src/lib.rs", "crate::units::Watts")]
        found = self._resolve(files, symbols, edges)
        self.assertEqual(found["crates/core/src/lib.rs->crate::units::Watts"], "s1")

    def test_a_crate_reference_never_leaves_its_own_crate(self) -> None:
        # No repository in the corpus has this shape, which is why it is
        # written down rather than waited for. Two members, only one of them
        # declaring `units` in a file: an ambiguity check sees a single match
        # and resolves the other member's reference to it. That is a wrong
        # answer, not an uncertain one, and `crate::` means this crate.
        files = [
            _file("crates/core/Cargo.toml"),
            _file("crates/core/src/units.rs"),
            _file("crates/tool/Cargo.toml"),
            _file("crates/tool/src/main.rs"),
        ]
        symbols = [_symbol("s1", "crates/core/src/units.rs", "units::Watts")]
        edges = [_edge("crates/tool/src/main.rs", "crate::units::Watts")]
        found = self._resolve(files, symbols, edges)
        self.assertIsNone(found["crates/tool/src/main.rs->crate::units::Watts"])

    def test_a_parent_module_reference_is_left_alone(self) -> None:
        # `super::mm` names a parent module, and a parent cannot be
        # identified from a file stem. Guessing one would resolve edges to
        # the wrong symbol instead of leaving them honestly unresolved.
        files = [_file("crates/core/Cargo.toml"), _file("crates/core/src/bench.rs")]
        symbols = [_symbol("s1", "crates/core/src/mm.rs", "mm")]
        edges = [_edge("crates/core/src/bench.rs", "super::mm")]
        self.assertIsNone(found_target(self._resolve(files, symbols, edges)))

    def test_relationships_other_than_imports_are_untouched(self) -> None:
        # A call names a binding, not a module, and resolving it needs the
        # import table this pass is the first half of.
        files = [_file("app/main.py", "Python")]
        symbols = [_symbol("s1", "app/store.py", "app.store.Ledger", "Python")]
        edges = [_edge("app/main.py", "app.store.Ledger", relationship="calls")]
        found = self._resolve(files, symbols, edges)
        self.assertIsNone(found["app/main.py->app.store.Ledger"])

    def test_an_edge_from_a_file_outside_the_snapshot_is_untouched(self) -> None:
        symbols = [_symbol("s1", "app/store.py", "app.store.Ledger", "Python")]
        edges = [_edge("vendor/other.py", "app.store.Ledger")]
        self.assertIsNone(self._resolve([], symbols, edges)["vendor/other.py->app.store.Ledger"])


def found_target(resolved: dict[str, str | None]) -> str | None:
    return next(iter(resolved.values()))
