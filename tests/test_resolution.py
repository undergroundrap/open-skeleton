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
from open_skeleton.resolution import resolve_call_targets, resolve_import_targets


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


def _sym(symbol_id: str, path: str, qualified_name: str, kind: str = "function") -> SymbolRecord:
    return SymbolRecord(
        symbol_id=symbol_id,
        snapshot_id="s",
        path=path,
        qualified_name=qualified_name,
        kind=kind,
        start_line=1,
        end_line=1,
        language="Python",
        analyzer="python-ast/v3",
    )


def _call(
    source_path: str, target_ref: str, source_symbol_id: str, analyzer: str = "python-ast/v3"
) -> EdgeRecord:
    return EdgeRecord(
        edge_id=f"{source_path}=>{target_ref}",
        snapshot_id="s",
        source_symbol_id=source_symbol_id,
        source_path=source_path,
        relationship="calls",
        target_ref=target_ref,
        target_symbol_id=None,
        evidence_id=None,
        analyzer=analyzer,
    )


class CallResolutionTests(TestCase):
    """A call binds to what the file said the name means, or to nothing.

    Most calls should bind to nothing. Measured across three repositories,
    53% to 83% of call targets name nothing the repository defines -- `len`,
    `clone`, `toBe` -- and another 17% are methods on `self`. About one call
    in six is unambiguously bindable, so an empty target is usually the
    correct answer rather than a gap.
    """

    def _resolve(
        self, symbols: Sequence[SymbolRecord], edges: Sequence[EdgeRecord]
    ) -> dict[str, str | None]:
        return {
            item.edge_id: item.target_symbol_id
            for item in resolve_call_targets(tuple(symbols), tuple(edges))
        }

    def test_a_call_to_an_imported_name_binds_to_the_import(self) -> None:
        symbols = [
            _sym("s1", "app/store.py", "app.store.open_ledger"),
            _sym("s2", "app/main.py", "app.main.run"),
        ]
        edges = [
            EdgeRecord(
                edge_id="i1",
                snapshot_id="s",
                source_symbol_id=None,
                source_path="app/main.py",
                relationship="imports",
                target_ref="app.store.open_ledger",
                target_symbol_id="s1",
                evidence_id=None,
                analyzer="python-ast/v3",
            ),
            _call("app/main.py", "open_ledger", "s2"),
        ]
        self.assertEqual(self._resolve(symbols, edges)["app/main.py=>open_ledger"], "s1")

    def test_a_call_to_a_name_the_same_module_defines_binds_to_it(self) -> None:
        symbols = [
            _sym("s1", "app/main.py", "app.main.helper"),
            _sym("s2", "app/main.py", "app.main.run"),
        ]
        self.assertEqual(
            self._resolve(symbols, [_call("app/main.py", "helper", "s2")])["app/main.py=>helper"],
            "s1",
        )

    def test_a_method_on_self_is_never_bound(self) -> None:
        # The type of `self` is not written at the call site, and picking the
        # one class that happens to define the name is a guess with a citation.
        symbols = [
            _sym("s1", "app/main.py", "app.main.render"),
            _sym("s2", "app/main.py", "app.main.run"),
        ]
        found = self._resolve(symbols, [_call("app/main.py", "self.render", "s2")])
        self.assertIsNone(found["app/main.py=>self.render"])

    def test_a_name_two_modules_define_is_not_adjudicated(self) -> None:
        symbols = [
            _sym("s1", "one/util.py", "one.util.helper"),
            _sym("s2", "two/util.py", "two.util.helper"),
            _sym("s3", "app/main.py", "app.main.run"),
        ]
        found = self._resolve(symbols, [_call("app/main.py", "helper", "s3")])
        self.assertIsNone(found["app/main.py=>helper"])

    def test_a_call_inside_a_module_does_not_escape_to_its_parent(self) -> None:
        # A module's own module is itself. Taking the parent let a call to
        # `new` inside `warmboot_game::graphics_ui` search `warmboot_game::`
        # and bind to a free function in `main.rs`; 254 of that repository's
        # 262 resolved calls were that, and `new` names every Rust constructor.
        symbols = [
            _sym("parent", "pkg/main.py", "pkg.new"),
            _sym("here", "pkg/graphics.py", "pkg.graphics", kind="module"),
        ]
        found = self._resolve(symbols, [_call("pkg/graphics.py", "new", "here")])
        self.assertIsNone(found["pkg/graphics.py=>new"])

    def test_a_reader_that_hides_receivers_has_its_calls_left_alone(self) -> None:
        # Given `text(1)` and `w.text()`, the Rust and TypeScript readers both
        # record `text`. Binding that to the free function of the same name is
        # a coin toss, and an agent walking the graph could not tell.
        symbols = [
            _sym("s1", "src/lib.rs", "lib::text"),
            _sym("s2", "src/lib.rs", "lib::caller"),
        ]
        edges = [_call("src/lib.rs", "text", "s2", analyzer="rust-lexical/v1")]
        self.assertIsNone(self._resolve(symbols, edges)["src/lib.rs=>text"])

    def test_an_import_relationship_is_not_touched_by_the_call_pass(self) -> None:
        edges = [
            EdgeRecord(
                edge_id="i1",
                snapshot_id="s",
                source_symbol_id=None,
                source_path="app/main.py",
                relationship="imports",
                target_ref="app.store",
                target_symbol_id=None,
                evidence_id=None,
                analyzer="python-ast/v3",
            )
        ]
        self.assertIsNone(self._resolve([], edges)["i1"])
