# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.rust_lexical import (
    RustLexicalAnalyzer,
    _call_sites,
    _constants,
    _declared_clap_flags,
    _declared_items,
    _error_surface,
    _impl_methods,
    _module_name,
    _module_names,
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
    """These abort the thread, and that is all they have in common.

    Reported as one number this read "4,192 panicking call sites" on a
    compiler of 72 files. 73% were assertions checking invariants and 2 were
    `todo!`, so the figure a reader wanted -- 175 bare `unwrap`s -- sat behind
    an aggregate 24 times larger. Technically true and useless.
    """

    def _panics(self, result: AnalysisResult) -> dict[str, tuple[str, str]]:
        """Every panic claim, keyed by the family word that identifies it."""

        found: dict[str, tuple[str, str]] = {}
        for item in result.claims:
            if item.category != "panic_site":
                continue
            for word in ("todo!", "unwrap`", "expect`", "panic!", "assertion"):
                if word in item.claim:
                    found[word] = (item.claim, item.importance)
                    break
        return found

    def test_an_unwrap_and_an_expect_are_not_the_same_finding(self) -> None:
        # `expect` records why the value must be there and `unwrap` records
        # nothing, so an audit starts with the second and must be able to.
        result = _analyze('fn f() { a.unwrap(); b.expect("why"); }')
        panics = self._panics(result)
        self.assertIn("1 call(s) to `unwrap`", panics["unwrap`"][0])
        self.assertIn("1 call(s) to `expect`", panics["expect`"][0])

    def test_unwrap_or_supplies_a_fallback_and_is_not_a_panic(self) -> None:
        result = _analyze("fn f() { a.unwrap_or(0); b.unwrap_or_else(|| 1); }")
        self.assertIsNone(_claim(result, "panic_site"))

    def test_panic_macros_are_counted_but_bare_identifiers_are_not(self) -> None:
        result = _analyze('fn f() { panic!("x"); let todo = 1; assert!(ok); }')
        panics = self._panics(result)
        self.assertIn("1 explicit `panic!`", panics["panic!"][0])
        self.assertIn("1 assertion site(s)", panics["assertion"][0])
        # `let todo = 1` is a binding, not `todo!`.
        self.assertNotIn("todo!", panics)

    def test_unwrap_as_a_plain_function_name_is_not_a_panic(self) -> None:
        result = _analyze("fn unwrap() {}\nfn f() { unwrap(); }")
        self.assertIsNone(_claim(result, "panic_site"))

    def test_assertions_are_ranked_below_the_findings_they_outnumber(self) -> None:
        # At `high` they filled the summary. An invariant check is the least
        # actionable thing in this category and the most numerous, which is
        # exactly the combination that displaces everything else.
        result = _analyze("fn f() { assert!(a); a.unwrap(); }")
        panics = self._panics(result)
        self.assertEqual(panics["assertion"][1], "low")
        self.assertEqual(panics["unwrap`"][1], "high")

    def test_the_whole_assertion_family_is_counted(self) -> None:
        # `assert_ne!` and `debug_assert!` were absent from the macro set,
        # undercounting this family by 50 on a real compiler.
        result = _analyze(
            "fn f() { assert!(a); assert_eq!(a, b); assert_ne!(a, c); debug_assert!(d); }"
        )
        self.assertIn("4 assertion site(s)", self._panics(result)["assertion"][0])

    def test_unfinished_work_is_reported_apart_from_everything_else(self) -> None:
        # Two `todo!`s in 72 files is a finding. Added to four thousand
        # assertions it is invisible.
        result = _analyze("fn f() { todo!(); unimplemented!(); assert!(x); }")
        panics = self._panics(result)
        self.assertIn("2 site(s) marked `todo!`", panics["todo!"][0])
        self.assertEqual(panics["todo!"][1], "high")


class PublicSurfaceTests(TestCase):
    """What a crate exposes, which is the primary fact about a library.

    The Python analyzer reports a module's public surface. The Rust one
    reported nothing, so what a caller may depend on was missing for every
    crate this engine read.
    """

    def _surface(self, source: str) -> set[str]:
        claim = next(
            (item for item in _analyze(source).claims if item.category == "public_api"), None
        )
        if claim is None:
            return set()
        body = claim.claim.split("public surface: ", 1)[1].split(". `pub(crate)`", 1)[0]
        return {part.strip() for part in body.split(",") if part.strip()}

    def test_each_kind_of_public_item_is_named(self) -> None:
        source = (
            "pub fn open() {}\npub struct Config;\npub enum Mode { A }\n"
            "pub trait Store {}\npub mod api;\npub const LIMIT: u32 = 5;\npub type Id = u64;\n"
        )
        self.assertEqual(
            self._surface(source), {"open", "Config", "Mode", "Store", "api", "LIMIT", "Id"}
        )

    def test_a_crate_visible_item_is_not_public_surface(self) -> None:
        # `pub(crate)` restricts a name to this crate. Counting it would tell
        # a reader they can depend on something no other crate can reach.
        source = "pub fn exposed() {}\npub(crate) fn internal() {}\npub(super) fn narrow() {}\n"
        self.assertEqual(self._surface(source), {"exposed"})

    def test_a_qualifier_between_pub_and_the_item_is_stepped_over(self) -> None:
        source = 'pub async fn fetch() {}\npub unsafe fn raw() {}\npub extern "C" fn ffi() {}\n'
        self.assertEqual(self._surface(source), {"fetch", "raw", "ffi"})

    def test_a_private_item_is_not_reported(self) -> None:
        self.assertEqual(self._surface("fn hidden() {}\nstruct Inner;\n"), set())


class EnvironmentReadTests(TestCase):
    """What a crate needs from its environment, which Rust reported as nothing.

    The Python analyzer has reported `os.getenv` since the beginning. A crate
    that will not start without `DATABASE_URL` said so in Python and stayed
    silent in Rust, which is a statement about the analyzer rather than about
    the code.
    """

    def _settings(self, source: str) -> dict[str, str]:
        result = _analyze(source)
        found = {}
        for claim in result.claims:
            if claim.category != "configuration_read":
                continue
            for word in claim.claim.split():
                if word.isupper() and len(word) > 2:
                    found[word] = "compile" if "compile time" in claim.claim else "run"
        return found

    def test_a_runtime_read_is_reported_as_one(self) -> None:
        source = 'fn go() { let a = std::env::var("APPDATA").unwrap(); }\n'
        self.assertEqual(self._settings(source), {"APPDATA": "run"})

    def test_a_compile_time_substitution_is_distinguished(self) -> None:
        # `env!` is replaced by the compiler, so the value is fixed in the
        # binary. Naming it the way `env::var` is named would tell an operator
        # to set a variable that nothing will ever read.
        source = 'fn go() { let a = env!("BUILD_ROOT"); }\n'
        self.assertEqual(self._settings(source), {"BUILD_ROOT": "compile"})

    def test_option_env_is_also_compile_time(self) -> None:
        source = 'fn go() { let a = option_env!("OPTIONAL_ROOT"); }\n'
        self.assertEqual(self._settings(source), {"OPTIONAL_ROOT": "compile"})

    def test_a_read_inside_a_comment_is_not_a_read(self) -> None:
        # The tokenizer is comment-safe, which is the whole reason this is
        # read from tokens rather than by matching the source text.
        source = 'fn go() {\n    // std::env::var("COMMENTED")\n    let a = 1;\n}\n'
        self.assertEqual(self._settings(source), {})

    def test_a_name_built_at_runtime_is_not_guessed(self) -> None:
        source = "fn go(key: &str) { let a = std::env::var(key); }\n"
        self.assertEqual(self._settings(source), {})

    def test_an_unrelated_var_call_is_not_an_environment_read(self) -> None:
        # `var` is an ordinary name. Only one reached through `env::` counts,
        # or every helper called `var` becomes configuration.
        source = 'fn go() { let a = config::var("NOT_ENV"); }\n'
        self.assertEqual(self._settings(source), {})


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

    def test_a_product_trait_implementation_publishes_a_contract(self) -> None:
        result = _analyze("impl Display for Machine {}\n", "lib.rs")
        self.assertIsNotNone(_claim(result, "trait_implementation"))

    def test_a_test_trait_implementation_is_not_the_product_contract(self) -> None:
        result = _analyze("impl Display for Fixture {}\n", "test_contract.rs")
        self.assertIsNone(_claim(result, "trait_implementation"))

    def test_a_macro_template_is_not_a_type(self) -> None:
        # `impl fmt::Display for $name` inside macro_rules declares nothing
        # about a type called `name`. Reporting one is a fabricated fact.
        source = (
            "macro_rules! m { ($name:ident) => { impl fmt::Display for $name { fn fmt() {} } }; }\n"
        )
        self.assertEqual(_trait_implementations(tokenize(source)), [])
        self.assertEqual(_impl_methods(tokenize(source)), [])


class CallSiteTests(TestCase):
    """Rust call edges, which did not exist until a real crate needed them.

    Every consumer that walks the call graph returned nothing for Rust because
    the analyzer emitted no `calls` edges at all. Capability tracing reported
    no verification for a 52-module crate, and the cause was not the tracing
    rules but that there was no graph to trace.
    """

    def test_a_plain_call_is_recorded(self) -> None:
        self.assertEqual(_call_sites(tokenize("fn f() { parse(x); }\n")), [("parse", 1)])

    def test_a_method_call_is_recorded(self) -> None:
        self.assertIn(
            "method", [name for name, _ in _call_sites(tokenize("fn f() { a.method(y); }\n"))]
        )

    def test_a_declaration_is_not_a_call(self) -> None:
        self.assertEqual(_call_sites(tokenize("fn run(x: u32) {}\n")), [])

    def test_a_macro_is_not_a_call(self) -> None:
        # `println!(...)` puts a bang between the name and the parenthesis.
        self.assertEqual(_call_sites(tokenize('fn f() { println!("hi"); }\n')), [])

    def test_control_flow_is_not_a_call(self) -> None:
        self.assertEqual(_call_sites(tokenize("fn f() { if (a) { } while (b) { } }\n")), [])

    def test_a_constructor_is_not_a_call(self) -> None:
        self.assertEqual(_call_sites(tokenize("fn f() { Some(y); Ok(z); }\n")), [])


class MacroTemplateTests(TestCase):
    """A macro body describes code; it does not declare it.

    `macro_rules! make { ($n:ident) => { pub struct Generated; }; }` declares no
    struct. It describes one a caller may ask for, under a name substituted at
    expansion. Recording it put types in the symbol index that nothing in the
    crate defines, and a reader cannot tell such an entry from a real one.

    Found by comparing against `syn`, which gets this boundary for free by
    treating a macro invocation as one opaque item and never descending.
    """

    def test_a_struct_inside_a_macro_definition_is_not_declared(self) -> None:
        source = "macro_rules! make {\n    ($n:ident) => { pub struct Generated; };\n}\n"
        self.assertEqual(_declared_items(tokenize(source)), [])

    def test_a_struct_inside_a_quote_body_is_not_declared(self) -> None:
        found = _declared_items(tokenize("quote! { pub struct Templated; }\n"))
        self.assertEqual(found, [])

    def test_a_real_declaration_beside_a_macro_survives(self) -> None:
        source = "macro_rules! make { () => { struct Hidden; }; }\npub struct Real;\n"
        self.assertEqual([name for _, name, _ in _declared_items(tokenize(source))], ["Real"])

    def test_an_implementation_inside_a_macro_body_is_not_recorded(self) -> None:
        source = "quote! { impl #generics Args for #ident #where_clause { } }\n"
        self.assertEqual(_trait_implementations(tokenize(source)), [])

    def test_a_lifetime_is_not_an_owner(self) -> None:
        # `impl Matcher for &'a Foo` reported the owner as `a` until lifetimes
        # became a token kind of their own.
        found = _trait_implementations(tokenize("impl<'a> Matcher for &'a Foo {}"))
        self.assertEqual([(owner, name) for owner, name, _ in found], [("Foo", "Matcher")])

    def test_a_path_qualified_owner_resolves_to_its_last_segment(self) -> None:
        found = _trait_implementations(tokenize("impl From<E> for std::io::Error {}"))
        self.assertEqual([(owner, name) for owner, name, _ in found], [("Error", "From")])

    def test_a_where_clause_is_not_part_of_the_type(self) -> None:
        found = _trait_implementations(tokenize("impl<T> Trait for Foo where T: Send {}"))
        self.assertEqual([(owner, name) for owner, name, _ in found], [("Foo", "Trait")])


class RawIdentifierTests(TestCase):
    """`r#async` names something `async`, not something `r`.

    Rust escapes a keyword used as a name by prefixing `r#`. Raw strings were
    handled and this was not, so `fn r#async(...)` was recorded as a function
    called `r` -- a name that appears nowhere in the crate. The character after
    the hash tells the two apart: `r#"` opens a string and `r#a` opens a name.
    """

    def test_a_raw_identifier_records_the_name_it_escapes(self) -> None:
        self.assertEqual(_declared_items(tokenize("fn r#async() {}")), [("function", "async", 1)])

    def test_a_raw_string_is_still_a_string(self) -> None:
        values = [item.value for item in tokenize('let s = r#"has "quote""#; fn after() {}')]
        self.assertIn("after", values)
        self.assertNotIn("quote", values)

    def test_a_raw_identifier_type_is_named_correctly(self) -> None:
        self.assertEqual(_declared_items(tokenize("struct r#type;")), [("struct", "type", 1)])


class CallSitePrecisionTests(TestCase):
    """Shapes that look like calls and are not, found by comparing against syn.

    Call edges feed capability tracing, and they were built here without any
    reference checking them. A single ripgrep run reported invented calls in
    104 of 110 files: attribute contents, visibility qualifiers, and enum
    variant constructions, none of which resolve to a definition anywhere in
    the crate.
    """

    def test_attribute_contents_are_not_calls(self) -> None:
        # `#[derive(Debug)]` and `#[cfg(not(any(unix, windows)))]` are an
        # identifier followed by a parenthesis, which is exactly a call's shape.
        source = "#[derive(Debug)]\n#[cfg(not(any(unix, windows)))]\nfn f() { parse(x); }\n"
        self.assertEqual([name for name, _ in _call_sites(tokenize(source))], ["parse"])

    def test_an_inner_attribute_is_also_skipped(self) -> None:
        source = "#![deny(warnings)]\nfn f() { work(x); }\n"
        self.assertEqual([name for name, _ in _call_sites(tokenize(source))], ["work"])

    def test_a_visibility_qualifier_is_not_a_call(self) -> None:
        self.assertEqual(_call_sites(tokenize("pub(crate) fn f() {}")), [])

    def test_an_enum_variant_construction_is_not_a_call(self) -> None:
        # `Mode::Search(x)` builds a value. It cannot be listed like `Some`
        # because it is the crate's own type, but Rust capitalises variants
        # and types and lints anything else, so the convention separates them.
        source = "fn f() { let m = Search(x); helper(y); }\n"
        self.assertEqual([name for name, _ in _call_sites(tokenize(source))], ["helper"])

    def test_a_turbofish_call_is_recorded(self) -> None:
        # `value.parse::<u64>()` puts the type between the name and the paren.
        self.assertEqual(_call_sites(tokenize("fn f() { v.parse::<u64>(); }")), [("parse", 1)])

    def test_a_nested_turbofish_is_recorded(self) -> None:
        found = _call_sites(tokenize("fn f() { collect::<Vec<String>>(x); }"))
        self.assertEqual([name for name, _ in found], ["collect"])

    def test_a_path_call_records_its_last_segment(self) -> None:
        self.assertEqual([name for name, _ in _call_sites(tokenize("fn f() { a::b(z); }"))], ["b"])


class ConstantPrecisionTests(TestCase):
    """`const fn` declares a function, and macro bodies follow one rule.

    Comparing the tunable index against `syn` found a constant named `fn` --
    `const fn parse(...)` puts the qualifier before the name, so a reader
    looking for `const <name>` finds the keyword. No crate contains a constant
    called `fn`.
    """

    def test_a_const_function_is_not_a_constant(self) -> None:
        self.assertEqual(_constants(tokenize("const fn parse(x: u8) -> u8 { x }")), {})

    def test_a_const_unsafe_function_is_not_a_constant(self) -> None:
        self.assertEqual(_constants(tokenize("pub const unsafe fn raw() {}")), {})

    def test_a_real_constant_survives(self) -> None:
        self.assertEqual(sorted(_constants(tokenize("const MAX: u32 = 5;"))), ["MAX"])

    def test_a_mutable_static_survives(self) -> None:
        self.assertEqual(sorted(_constants(tokenize("static mut N: usize = 0;"))), ["N"])

    def test_a_constant_inside_a_macro_body_follows_the_module_rule(self) -> None:
        # `_declared_items` and `_trait_implementations` both skip macro bodies.
        # Whether a body is a template or real code cannot be told lexically, so
        # one rule applies rather than a different answer per extractor.
        source = 'rgtest!(name, |dir| { const HAYSTACK: &str = "x"; });\nconst REAL: u8 = 1;\n'
        self.assertEqual(sorted(_constants(tokenize(source))), ["REAL"])


class ModulePathTests(TestCase):
    """A Rust module path is the language's, not the filesystem's.

    Joining directories verbatim produced
    `crates::warmboot-core::src::catalog::layout` -- a name that appears in no
    `use` statement anywhere and that a reader cannot paste into one. Cargo
    does not put `src` in a module path, and a crate directory named
    `warmboot-core` is `warmboot_core` to the language.

    The receipt still names the file exactly. Only the module path changes,
    because a path nobody can use is a name this engine made up.
    """

    def test_a_workspace_crate_drops_its_directory_scaffolding(self) -> None:
        found = _module_name("crates/warmboot-core/src/catalog/layout.rs")
        self.assertEqual(found, "warmboot_core::catalog::layout")

    def test_a_single_crate_root_is_the_crate(self) -> None:
        self.assertEqual(_module_name("src/main.rs"), "crate")
        self.assertEqual(_module_name("src/lib.rs"), "crate")

    def test_a_module_beside_the_root_keeps_its_own_name(self) -> None:
        self.assertEqual(_module_name("src/power.rs"), "power")

    def test_a_hyphenated_crate_becomes_an_identifier(self) -> None:
        # `warmboot-core` is a package name; `warmboot_core` is the module.
        self.assertEqual(_module_name("crates/cli/src/lib.rs"), "cli")
        self.assertTrue("-" not in _module_name("crates/a-b/src/lib.rs"))

    def test_an_integration_test_is_named_as_its_own_crate(self) -> None:
        # `tests/`, `benches/` and `examples/` each compile as a separate crate
        # rather than as a module of the one beside them, so the file names it
        # and the directories around it are Cargo's layout rather than a path.
        # This asserted `tests::compat` until the diagram showed
        # `crates::warmboot_core::tests::compat`, which names three modules
        # that do not exist.
        self.assertEqual(_module_name("tests/compat.rs"), "compat")
        self.assertEqual(_module_name("crates/warmboot-core/tests/compat.rs"), "compat")
        self.assertEqual(_module_name("benches/bench.rs"), "bench")
        self.assertEqual(_module_name("examples/demo.rs"), "demo")


class ConfiguredDuplicateClaimTests(TestCase):
    """One fact stated by two sites keeps both receipts.

    `#[cfg]` is how Rust writes a platform split: the trait is implemented once
    for Windows and once for everything else, both impls are real, and exactly
    one compiles per platform. The claim text is identical, so both records
    carry the same identifier.

    The ledger keys claims by that identifier and rewrote the evidence link per
    claim, so the second impl deleted the first one's receipt. The run reported
    635 claims, stored 634, and cited whichever impl happened to be written
    last -- which on the other platform is the dead one.
    """

    SOURCE = """\
pub trait FileReadAdapter {
    fn read_text(&mut self) -> String;
}

pub struct HostFileReadAdapter;

#[cfg(not(windows))]
impl FileReadAdapter for HostFileReadAdapter {
    fn read_text(&mut self) -> String {
        String::new()
    }
}

#[cfg(windows)]
impl FileReadAdapter for HostFileReadAdapter {
    fn read_text(&mut self) -> String {
        String::from("windows")
    }
}
"""

    def _result(self) -> AnalysisResult:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "lib.rs").write_text(self.SOURCE, encoding="utf-8")
            return analyze_snapshot(scan_repository(root))

    def test_the_claim_is_reported_once(self) -> None:
        result = self._result()
        matching = [item for item in result.claims if "implements FileReadAdapter" in item.claim]
        self.assertEqual(len(matching), 1)

    def test_both_implementations_are_still_cited(self) -> None:
        result = self._result()
        claim = next(item for item in result.claims if "implements FileReadAdapter" in item.claim)
        lines = {
            record.start_line
            for record in result.evidence
            if record.evidence_id in claim.supporting_evidence
        }
        # Line 8 is the non-Windows impl and line 15 the Windows one. Citing
        # one of them would be citing dead code on the other platform.
        self.assertEqual(lines, {8, 15})

    def test_no_claim_identifier_is_reported_twice(self) -> None:
        result = self._result()
        identifiers = [item.claim_id for item in result.claims]
        self.assertEqual(len(identifiers), len(set(identifiers)))


class NegatedCallTests(TestCase):
    """A negated condition is not a macro body.

    `macro_rules! name {` is the one Rust form that puts an identifier
    between the bang and the body, and the macro-span detector accepted any
    identifier there. `if !ready(x)` has the same four-token shape --
    identifier, bang, identifier, delimiter -- so the whole condition was
    read as a macro body and every call inside it was discarded.

    `syn` counted 19 calls across 13 files of one crate that this never
    reported. Capability tracing runs on call edges, so the cost was not a
    missing row: a capability exercised only from inside such a condition
    reported no verifying reference.
    """

    def test_a_negated_call_is_recorded(self) -> None:
        found = {name for name, _ in _call_sites(tokenize("fn a() { if !is_value(name) {} }"))}
        self.assertEqual(found, {"is_value"})

    def test_calls_nested_in_a_negated_condition_survive(self) -> None:
        source = "fn a() { while !ready(check(x)) { step(y); } }"
        found = {name for name, _ in _call_sites(tokenize(source))}
        self.assertEqual(found, {"ready", "check", "step"})

    def test_a_parenthesised_negation_is_not_a_macro_invocation(self) -> None:
        # `if !(a || b)` has the same three-token shape as `vec![...]`:
        # identifier, bang, delimiter. A keyword is never a macro name.
        source = "fn a() { if !(case.is_changed() || other.is_changed()) {} }"
        found = {name for name, _ in _call_sites(tokenize(source))}
        self.assertEqual(found, {"is_changed"})

    def test_a_real_macro_invocation_is_still_a_macro(self) -> None:
        source = "fn a() { let v = vec![1, 2]; step(x); }"
        found = {name for name, _ in _call_sites(tokenize(source))}
        self.assertEqual(found, {"step"})

    def test_a_macro_definition_body_is_still_excluded(self) -> None:
        # The exclusion this narrows exists for a reason: a macro body holds
        # a template, and reading it as code reported implementations on
        # substitution placeholders.
        source = "macro_rules! shout { ($n:ident) => { impl Loud for Thing {} }; }"
        self.assertEqual(_trait_implementations(tokenize(source)), [])
        self.assertEqual(_call_sites(tokenize(source)), [])

    def test_a_macro_invocation_body_is_still_excluded(self) -> None:
        source = "fn a() { quote! { impl Loud for Thing {} } }"
        self.assertEqual(_trait_implementations(tokenize(source)), [])


class CrateRootCollisionTests(TestCase):
    """Two crate roots in one package do not share one name.

    A package holding both `src/lib.rs` and `src/main.rs` has a library crate
    and a binary crate, and Cargo names both after the package, so both files
    reduced to the same Rust path. The document then carried two claims with
    the same subject and different numbers -- `cranelift_feasibility declares
    1 fallible function(s)` above `cranelift_feasibility declares 16` -- which
    reads as a contradiction and is really two crates.

    Nothing automated caught it. Both claims are faithful to their own file,
    so the ledger is consistent and the coherence checks pass; only a reader
    sees the collision.
    """

    def test_a_library_and_binary_root_are_told_apart(self) -> None:
        found = _module_names(["pkg/src/lib.rs", "pkg/src/main.rs"])
        self.assertEqual(found["pkg/src/lib.rs"], "pkg")
        self.assertEqual(found["pkg/src/main.rs"], "pkg::main")

    def test_the_library_keeps_the_bare_crate_name(self) -> None:
        # That is the path other crates really use to reach its items, so it
        # is the one name that must not move.
        found = _module_names(["crates/core/src/lib.rs", "crates/core/src/main.rs"])
        self.assertEqual(found["crates/core/src/lib.rs"], "core")

    def test_names_that_do_not_collide_are_unchanged(self) -> None:
        paths = ["crates/core/src/lib.rs", "crates/core/src/parser.rs", "src/main.rs"]
        found = _module_names(paths)
        self.assertEqual(found["crates/core/src/parser.rs"], "core::parser")
        self.assertEqual(found["src/main.rs"], "crate")

    def test_every_file_resolves_to_a_distinct_name(self) -> None:
        paths = ["a/src/lib.rs", "a/src/main.rs", "b/src/lib.rs", "b/src/main.rs"]
        found = _module_names(paths)
        self.assertEqual(len(set(found.values())), len(paths))


class TestRoleErrorSurfaceTests(TestCase):
    """A crate's error surface is not what its integration tests declare.

    `crates/warmboot-core/tests/compat.rs` declares its own fallible helper,
    and reporting it made that file the whole of what warmboot appeared to
    say about how it handles failure. Python re-files a test file's claims by
    category at one choke point for exactly this reason; the rule had never
    crossed to this reader.
    """

    SOURCE = "pub fn load() -> Result<u8, Error> { Ok(1) }\n"

    def _categories(self, name: str) -> set[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.SOURCE, encoding="utf-8")
            result = RustLexicalAnalyzer().analyze(scan_repository(root))
        return {item.category for item in result.claims}

    def _pipeline_categories(self, name: str) -> set[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.SOURCE, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
        return {item.category for item in result.claims}

    def test_application_code_declares_an_error_surface(self) -> None:
        self.assertIn("error_surface", self._categories("src/lib.rs"))
        self.assertIn("error_surface", self._pipeline_categories("src/lib.rs"))

    def test_a_suite_s_error_handling_is_reported_as_the_suite_s(self) -> None:
        # This reader used to drop the claim, which fixed the wrong half of
        # the problem twice: it named the test role only, so a benchmark's
        # error surface was still the crate's, and it lost a true fact, since
        # what a suite absorbs is worth knowing under the suite's name.
        #
        # The reader now states what it sees and `analyze_snapshot` files it
        # by the role of the evidence. What must not happen -- warmboot
        # appearing to describe its failure handling out of `tests/compat.rs`
        # -- still does not.
        self.assertIn("error_surface", self._categories("tests/compat.rs"))
        categories = self._pipeline_categories("tests/compat.rs")
        self.assertIn("test_error_surface", categories)
        self.assertNotIn("error_surface", categories)

    def test_a_benchmark_s_error_surface_is_not_the_crate_s(self) -> None:
        categories = self._pipeline_categories("benchmarks/reference.rs")
        self.assertIn("harness_error_surface", categories)
        self.assertNotIn("error_surface", categories)


class ClapCommandLineTests(TestCase):
    """A command line is a tool's whole interface, in any language.

    Reading Python's and not Rust's made `command_line_interface` fire for
    exactly one repository, which is the shape of an analyzer written against
    one codebase rather than a property of the world.
    """

    def _flags(self, source: str) -> dict[str, int]:
        return _declared_clap_flags(tokenize(source))

    DERIVED = """\
#[derive(Parser)]
pub struct Cli {
    /// Repository root.
    #[arg(long)]
    pub repo: Option<PathBuf>,

    #[arg(long)]
    pub github_repo: String,

    #[arg(long = "listen-port")]
    pub port: u16,
}
"""

    def test_a_bare_long_derives_the_flag_from_its_field(self) -> None:
        flags = self._flags(self.DERIVED)
        self.assertIn("--repo", flags)

    def test_an_underscore_becomes_a_hyphen_the_way_clap_does_it(self) -> None:
        self.assertIn("--github-repo", self._flags(self.DERIVED))

    def test_an_explicit_name_is_quoted_as_written(self) -> None:
        flags = self._flags(self.DERIVED)
        self.assertIn("--listen-port", flags)
        self.assertNotIn("--port", flags)

    def test_rename_all_stops_the_derivation_rather_than_guessing(self) -> None:
        # A flag printed under the wrong naming rule is one nobody can type,
        # which is worse than an omission.
        source = self.DERIVED.replace(
            "#[derive(Parser)]", '#[derive(Parser)]\n#[command(rename_all = "snake_case")]'
        )
        flags = self._flags(source)
        self.assertNotIn("--github-repo", flags)
        self.assertIn("--listen-port", flags)

    def test_an_attribute_above_a_function_names_no_field(self) -> None:
        self.assertEqual(self._flags("#[arg(long)]\npub fn run() {}\n"), {})

    def test_a_crate_without_clap_declares_nothing(self) -> None:
        self.assertEqual(self._flags("pub fn main() {}\n"), {})

    def test_the_flag_is_recorded_with_its_line(self) -> None:
        flags = self._flags(self.DERIVED)
        self.assertEqual(flags["--repo"], 4)

    def test_the_claim_reaches_the_pipeline_and_the_concordance(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "config.rs").write_text(self.DERIVED, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            claim = next(
                item for item in result.claims if item.category == "command_line_interface"
            )
            self.assertIn("`--github-repo`", claim.claim)
            index: dict[str, int] = {}
            for symbol in result.symbols:
                index.update(symbol.metadata.get("name_index", {}))
            self.assertIn("--github-repo", index)


class PublicSurfaceRoleTests(TestCase):
    """A `pub` item in a test file is public to the suite, not to a consumer.

    The engine's own audit flagged `test_skeletons` as declaring the crate's
    public surface. Reporting it says a caller can depend on something no
    caller can reach, and the `fn main` claim beside it already applied this
    rule.
    """

    SOURCE = "pub fn program_to_test_skeletons() {}\n"

    def _categories(self, path: str) -> set[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.SOURCE, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            return {item.category for item in result.claims}

    def test_a_library_file_declares_public_surface(self) -> None:
        self.assertIn("public_api", self._categories("src/lib.rs"))

    def test_a_test_file_does_not(self) -> None:
        self.assertNotIn("public_api", self._categories("tests/skeletons.rs"))


class ValuelessConstantTests(TestCase):
    """A constant whose value was never seen must not reach the value panel.

    A Rust `static` can be declared in one place and assigned in another, so
    this reader records the name and the site without a value. The string
    panel subscripts the value directly, and routing such an entry there
    crashed the whole document for one repository -- a case the panel's own
    comment had warned about.
    """

    def test_a_constant_without_a_value_stays_out_of_the_value_panel(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "lib.rs").write_text(
                'pub static LATE: &str;\npub const NAME: &str = "pool";\npub const MAX: u32 = 7;\n',
                encoding="utf-8",
            )
            result = analyze_snapshot(scan_repository(root))
        for symbol in result.symbols:
            for entry in (symbol.metadata.get("string_constants") or {}).values():
                self.assertIn("value", entry)
