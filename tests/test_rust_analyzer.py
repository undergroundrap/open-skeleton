# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analyzers.rust_lexical import RustLexicalAnalyzer, tokenize
from open_skeleton.models import AnalysisResult
from open_skeleton.scanner import scan_repository


def _analyze(source: str, name: str = "lib.rs") -> AnalysisResult:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / name).write_text(source, encoding="utf-8")
        return RustLexicalAnalyzer().analyze(scan_repository(root))


def _claim(result: AnalysisResult, category: str) -> str | None:
    for item in result.claims:
        if item.category == category:
            return item.claim
    return None


class TokenizerTraps(TestCase):
    """The three Rust lexing traps, each of which silently corrupts every count."""

    def test_block_comments_nest(self) -> None:
        tokens = tokenize("/* outer /* inner */ still comment */ real")
        self.assertEqual([item.value for item in tokens], ["real"])

    def test_raw_string_with_hashes_is_skipped_whole(self) -> None:
        tokens = tokenize('let a = r#"contains " and unsafe"#; after')
        values = [item.value for item in tokens]
        self.assertNotIn("unsafe", values)
        self.assertIn("after", values)

    def test_lifetime_is_not_a_character_literal(self) -> None:
        # A scanner that reads `'a` as an open quote swallows the rest of the file.
        tokens = tokenize("fn f<'a>(x: &'a str) -> &'static str { body }")
        self.assertIn("body", [item.value for item in tokens])

    def test_character_literal_is_skipped(self) -> None:
        tokens = tokenize("let c = 'x'; let d = '\\n'; after")
        self.assertIn("after", [item.value for item in tokens])

    def test_line_numbers_survive_comments_and_strings(self) -> None:
        tokens = tokenize('// one\n/* two\nthree */\n"four\\nstill"\nmarker')
        marker = next(item for item in tokens if item.value == "marker")
        self.assertEqual(marker.line, 5)


class UnsafeCensusTests(TestCase):
    def test_unsafe_in_a_string_literal_is_not_counted(self) -> None:
        # Exactly the case found in hum-lang: capability names mentioning unsafe.
        result = _analyze('fn f() { let names = ["unsafe", "ffi"]; }')
        claim = _claim(result, "unsafe_surface")
        assert claim is not None
        self.assertIn("No `unsafe` keyword", claim)

    def test_unsafe_in_a_comment_is_not_counted(self) -> None:
        result = _analyze("// this function is unsafe to call\nfn f() {}")
        claim = _claim(result, "unsafe_surface")
        assert claim is not None
        self.assertIn("No `unsafe` keyword", claim)

    def test_real_unsafe_blocks_are_counted_with_receipts(self) -> None:
        result = _analyze('fn f() {\n    unsafe { g() };\n}\nunsafe extern "C" { fn g(); }')
        claim = _claim(result, "unsafe_surface")
        assert claim is not None
        self.assertIn("2 `unsafe` keyword sites", claim)
        receipts = [item for item in result.evidence if item.evidence_kind == "unsafe_surface"]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(sorted(item.start_line for item in receipts), [2, 4])


class PanicCensusTests(TestCase):
    def test_unwrap_and_expect_are_counted(self) -> None:
        result = _analyze('fn f() { a.unwrap(); b.expect("why"); }')
        claim = _claim(result, "panic_site")
        assert claim is not None
        self.assertIn("2 panicking call sites", claim)

    def test_unwrap_or_supplies_a_fallback_and_is_not_a_panic(self) -> None:
        result = _analyze("fn f() { a.unwrap_or(0); b.unwrap_or_else(|| 1); }")
        self.assertIsNone(_claim(result, "panic_site"))

    def test_panic_macros_are_counted_but_bare_identifiers_are_not(self) -> None:
        result = _analyze('fn f() { panic!("x"); let todo = 1; assert!(ok); }')
        claim = _claim(result, "panic_site")
        assert claim is not None
        self.assertIn("2 panicking call sites", claim)

    def test_unwrap_as_a_plain_function_name_is_not_a_panic(self) -> None:
        result = _analyze("fn unwrap() {}\nfn f() { unwrap(); }")
        self.assertIsNone(_claim(result, "panic_site"))


class ItemAndImportTests(TestCase):
    def test_items_become_symbols(self) -> None:
        result = _analyze("struct S;\nenum E { A }\ntrait T {}\nfn f() {}")
        kinds = {item.kind for item in result.symbols}
        self.assertEqual(kinds, {"module", "struct", "enum", "trait", "function"})

    def test_use_statements_become_import_edges(self) -> None:
        result = _analyze("use std::collections::HashMap;\nuse crate::thing as other;")
        targets = {item.target_ref for item in result.edges if item.relationship == "imports"}
        self.assertEqual(targets, {"std::collections::HashMap", "crate::thing"})

    def test_fn_main_is_reported_as_an_entry_point(self) -> None:
        result = _analyze("fn main() {}", name="main.rs")
        claim = _claim(result, "application_entry")
        assert claim is not None
        self.assertIn("fn main", claim)

    def test_test_attribute_census(self) -> None:
        result = _analyze("#[cfg(test)]\nmod t {\n  #[test]\n  fn a() {}\n}")
        claim = _claim(result, "testing")
        assert claim is not None
        self.assertIn("1 `#[test]`", claim)

    def test_absent_tests_are_reported_as_a_gap(self) -> None:
        result = _analyze("fn f() {}")
        self.assertIsNotNone(_claim(result, "testing_gap"))


class CoverageTests(TestCase):
    def test_every_eligible_file_is_analyzed_and_yields_are_attributable(self) -> None:
        result = _analyze("fn main() { a.unwrap(); }", name="main.rs")
        coverage = result.coverage[0]
        self.assertEqual(coverage.language, "Rust")
        self.assertEqual(coverage.eligible_files, 1)
        self.assertEqual(coverage.analyzed_files, 1)
        self.assertEqual(coverage.failed_files, 0)

    def test_output_is_deterministic(self) -> None:
        source = "use a::b;\nfn main() { c.unwrap(); unsafe { d() } }"
        first = _analyze(source, name="main.rs")
        second = _analyze(source, name="main.rs")
        self.assertEqual(
            [item.claim for item in first.claims], [item.claim for item in second.claims]
        )
