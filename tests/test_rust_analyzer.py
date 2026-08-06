# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analyzers.rust_lexical import (
    RustLexicalAnalyzer,
    _constants,
    _error_surface,
    _impl_methods,
    _mutable_statics,
    _name_index,
    _struct_fields,
    _trait_implementations,
    tokenize,
)
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


class RustDeclarationTests(TestCase):
    """Rust needs the same extraction families Python has, not fewer."""

    def test_constants_carry_their_type_and_value(self) -> None:
        found = _constants(tokenize("pub const MAX: u32 = 5;\nstatic mut N: usize = 0;\n"))
        self.assertEqual(found["MAX"], {"line": 1, "kind": "const", "type": "u32", "value": "5"})
        self.assertEqual(found["N"]["kind"], "static")

    def test_a_numeric_literal_is_reassembled_from_its_characters(self) -> None:
        # The tokenizer emits every non-identifier character separately, so
        # 22.0 arrives as four tokens and a naive join renders "2 2 . 0".
        found = _constants(tokenize("const AMBIENT: f32 = 22.0;\n"))
        self.assertEqual(found["AMBIENT"]["value"], "22.0")

    def test_an_array_type_keeps_its_internal_semicolon(self) -> None:
        found = _constants(tokenize('const NAMES: [&str; 2] = ["a", "b"];\n'))
        self.assertEqual(found["NAMES"]["type"], "[&str;2]")

    def test_a_string_valued_constant_recovers_its_contents(self) -> None:
        # This once asserted the opposite: the tokenizer discarded string
        # bodies, so `"1.0"` survived only as punctuation and the value was
        # dropped rather than printed as garbage. Keeping literals as typed
        # tokens makes the real value available, which is what a reader of a
        # configuration constant actually wants.
        self.assertEqual(_constants(tokenize('static V: &str = "1.0";\n'))["V"]["value"], "1.0")

    def test_struct_fields_are_recorded_with_their_types(self) -> None:
        found = _struct_fields(
            tokenize("pub struct M {\n  pub id: String,\n  parts: Vec<Part>,\n}\n")
        )
        fields = {item["name"]: item["annotation"] for item in found["M"]["fields"]}
        self.assertEqual(fields, {"id": "String", "parts": "Vec<Part>"})

    def test_a_unit_struct_has_no_fields(self) -> None:
        self.assertEqual(_struct_fields(tokenize("struct Unit;\n")), {})

    def test_the_name_index_covers_rust_sources(self) -> None:
        # Rust contributed nothing to the concordance, which is much of why a
        # Rust repository looked empty beside a Python one.
        names = _name_index(tokenize("fn resolve_tick(delta: f32) -> f32 { delta }\n"))
        self.assertIn("resolve_tick", names)
        self.assertIn("delta", names)


class RustSharedStateTests(TestCase):
    """Rust's answer to a module-level dict is a static, and it was unread."""

    def test_a_static_mut_is_shared_without_synchronisation(self) -> None:
        found = _mutable_statics(tokenize("static mut COUNTER: usize = 0;\n"))
        self.assertEqual(found[0][0], "COUNTER")
        self.assertIn("no synchronisation", found[0][2])

    def test_a_static_holding_a_lock_is_still_shared_state(self) -> None:
        found = _mutable_statics(tokenize("static R: Mutex<Vec<u8>> = Mutex::new(Vec::new());\n"))
        self.assertEqual(found[0][0], "R")
        self.assertIn("Mutex", found[0][2])

    def test_an_immutable_static_is_not_shared_state(self) -> None:
        # A constant table is read-only. Reporting it as mutable state would
        # be the same error as calling a lookup table a queue.
        self.assertEqual(_mutable_statics(tokenize('static NAMES: [&str; 2] = ["a", "b"];\n')), [])

    def test_impl_methods_are_attributed_to_the_type_not_the_trait(self) -> None:
        found = _impl_methods(tokenize("impl Display for Machine {\n  fn fmt(&self) {}\n}\n"))
        self.assertEqual(found, [("Machine", "fmt", 2)])

    def test_an_inherent_impl_attributes_to_its_type(self) -> None:
        found = _impl_methods(tokenize("impl Machine {\n  pub fn boot(&self) {}\n}\n"))
        self.assertEqual(found, [("Machine", "boot", 2)])


class RustErrorAndTraitTests(TestCase):
    """Rust states its failure surface in the type system, not in raises."""

    def test_a_result_signature_is_fallible_and_its_error_type_is_named(self) -> None:
        found = _error_surface(tokenize("fn boot() -> Result<Machine, BootError> { Ok(m) }\n"))
        self.assertEqual(found["fallible_functions"], [("boot", 1)])
        self.assertEqual(found["error_types"], {"BootError": 1})

    def test_question_marks_are_counted_as_propagation(self) -> None:
        found = _error_surface(
            tokenize("fn go() -> Result<()> {\n  let a = f()?;\n  g()?;\n  Ok(())\n}\n")
        )
        self.assertEqual(found["propagation_sites"], 2)

    def test_an_infallible_function_is_not_counted(self) -> None:
        self.assertEqual(
            _error_surface(tokenize("fn plain(x: u8) -> u8 { x }\n"))["fallible_functions"], []
        )

    def test_a_generic_argument_is_not_mistaken_for_the_trait(self) -> None:
        # `impl From<Error> for BootError` implements From. Reading the last
        # name before `for` would call the trait Error.
        self.assertEqual(
            _trait_implementations(tokenize("impl From<Error> for BootError {}\n")),
            [("BootError", "From", 1)],
        )

    def test_a_path_qualified_trait_resolves_to_its_final_segment(self) -> None:
        self.assertEqual(
            _trait_implementations(tokenize("impl std::fmt::Display for M {}\n")),
            [("M", "Display", 1)],
        )

    def test_an_inherent_impl_declares_no_contract(self) -> None:
        self.assertEqual(_trait_implementations(tokenize("impl Machine { fn a() {} }\n")), [])

    def test_a_macro_template_is_not_a_type(self) -> None:
        # `impl fmt::Display for $name` inside macro_rules declares nothing
        # about a type called `name`. Reporting one is a fabricated fact.
        source = (
            "macro_rules! m { ($name:ident) => { impl fmt::Display for $name { fn fmt() {} } }; }\n"
        )
        self.assertEqual(_trait_implementations(tokenize(source)), [])
        self.assertEqual(_impl_methods(tokenize(source)), [])
