# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Java declaration reading.

The declaration half of this reader is checked against `javac -Xprint` by
`benchmarks/differential/run_java_differential.py`, which agrees exactly on
all 3,064 files of `java.base`. The cases pinned here are the ones that
differential cannot reach: annotations, which the reference silently drops
whenever the classpath is incomplete, and the shapes that a corpus happens
not to contain.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.java_lexical import (
    JavaMember,
    declared_members,
    declared_types,
    imported_types,
    package_name,
    tokenize,
)
from open_skeleton.models import AnalysisResult
from open_skeleton.scanner import scan_repository


def _analyze(sources: dict[str, str]) -> AnalysisResult:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        for name, body in sources.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        return analyze_snapshot(scan_repository(root))


class TokenizerTests(TestCase):
    def test_a_text_block_is_one_token(self) -> None:
        # A text block may contain unescaped quotes and spans lines. Reading
        # it as three empty strings makes the tokenizer read its contents as
        # code; the same defect in the TypeScript reader swallowed the rest
        # of every file containing a regex with a quote in it.
        source = 'class A { String q = """\n  a "quoted" word; class B {}\n  """; int after = 1; }'
        kinds = [token.value for token in tokenize(source) if token.kind == "identifier"]
        self.assertIn("after", kinds)
        self.assertNotIn("B", kinds)

    def test_a_block_comment_does_not_nest(self) -> None:
        # Java closes at the first `*/`, unlike Rust. Treating them as nesting
        # swallows the code after a comment that merely mentions `/*`.
        source = "class A { /* mentions /* inside */ int after = 1; }"
        values = [token.value for token in tokenize(source) if token.kind == "identifier"]
        self.assertIn("after", values)

    def test_a_quote_inside_a_char_literal_does_not_open_a_string(self) -> None:
        source = "class A { char c = '\"'; int after = 1; }"
        values = [token.value for token in tokenize(source) if token.kind == "identifier"]
        self.assertIn("after", values)

    def test_an_annotation_type_declaration_is_not_an_annotation_use(self) -> None:
        # `@interface` declares a type. Absorbing the keyword into an
        # annotation token hid every such declaration -- all twelve in
        # `java.lang` -- because the `interface` token had been consumed.
        found = declared_types(tokenize("public @interface Override {}"))
        self.assertEqual(
            [(item.name, item.kind) for item in found], [("Override", "annotation_type")]
        )


class DeclaredTypeTests(TestCase):
    def test_nested_types_are_qualified_by_their_owner(self) -> None:
        found = declared_types(tokenize("class Outer { static class Member { class Deep {} } }"))
        self.assertEqual(
            [item.name for item in found], ["Outer", "Outer.Member", "Outer.Member.Deep"]
        )

    def test_a_class_declared_in_a_method_body_is_marked_local(self) -> None:
        # A local class is not reachable by any qualified name, so treating
        # one as a member invents `Outer.Local` as part of a surface it never
        # joins. `javac -Xprint` does not print them at all.
        source = "class Outer { void go() { class Local {} } static class Member {} }"
        found = {item.name: item.local for item in declared_types(tokenize(source))}
        self.assertTrue(found["Outer.Local"])
        self.assertFalse(found["Outer.Member"])

    def test_an_anonymous_class_declares_no_type(self) -> None:
        source = "class A { void go() { Runnable r = new Runnable() { public void run() {} }; } }"
        self.assertEqual([item.name for item in declared_types(tokenize(source))], ["A"])

    def test_record_is_a_type_only_when_a_name_and_parameters_follow(self) -> None:
        # `record` is contextual. Treating it as a keyword invents a type
        # from an ordinary variable called `record`.
        declared = declared_types(tokenize("class A { record Point(int x, int y) {} }"))
        self.assertEqual([item.kind for item in declared], ["class", "record"])
        variable = declared_types(tokenize("class A { void go() { var record = load(); } }"))
        self.assertEqual([item.name for item in variable], ["A"])

    def test_supertypes_exclude_generic_arguments(self) -> None:
        source = "class A extends Base implements List<String>, Runnable {}"
        found = declared_types(tokenize(source))[0]
        self.assertEqual(found.supertypes, ("Base", "List", "Runnable"))

    def test_a_permits_clause_does_not_name_a_supertype(self) -> None:
        source = "public sealed interface Shape permits Circle, Square {}"
        self.assertEqual(declared_types(tokenize(source))[0].supertypes, ())

    def test_a_package_and_its_imports_are_read(self) -> None:
        source = "package a.b.c;\nimport java.util.List;\nimport static x.Y.z;\nclass A {}"
        tokens = tokenize(source)
        self.assertEqual(package_name(tokens), "a.b.c")
        self.assertEqual([name for name, _ in imported_types(tokens)], ["java.util.List", "x.Y.z"])


class DeclaredMemberTests(TestCase):
    SOURCE = """\
package p;
public class Service {
    private static int counter = 0;
    private final String name = "x";
    public static void main(String[] args) { int local = 1; }
    @Test public void checks() { if (ready) { go(); } }
    @GetMapping("/health") public String health() { return "ok"; }
    protected int limit() { return 1; }
}
"""

    def _members(self) -> dict[str, JavaMember]:
        return {item.name: item for item in declared_members(tokenize(self.SOURCE))}

    def test_methods_and_fields_are_separated(self) -> None:
        members = self._members()
        self.assertEqual(members["counter"].kind, "field")
        self.assertEqual(members["health"].kind, "method")

    def test_a_local_variable_is_not_a_member(self) -> None:
        self.assertNotIn("local", self._members())

    def test_a_control_keyword_does_not_declare_a_method(self) -> None:
        # `if (ready)` carries a parameter list too.
        self.assertNotIn("if", self._members())

    def test_an_annotation_argument_does_not_hide_the_method(self) -> None:
        # The annotation's own parentheses precede the method's, and reading
        # the first pair found made every annotated route method disappear
        # while unannotated ones were read correctly.
        members = self._members()
        self.assertIn("health", members)
        self.assertEqual(members["health"].annotation_arguments, (("GetMapping", "/health"),))


class JavaClaimTests(TestCase):
    SOURCES = {
        "Greeter.java": (
            "package p;\npublic interface Greeter {\n"
            "    String greet(String n);\n    private void helper() {}\n}\n"
        ),
        "Service.java": (
            "package p;\n\n@RestController\npublic class Service implements Greeter {\n"
            "    private static int counter = 0;\n"
            "    private static final int LIMIT = 5;\n"
            "    public String greet(String n) { return n; }\n"
            '    @GetMapping("/health") public String health() { return "ok"; }\n'
            '    @RequestMapping("/users") public String users() { return ""; }\n'
            "    @Test public void checks() {}\n"
            "    public static void main(String[] a) {}\n}\n"
        ),
    }

    def _claims(self) -> dict[str, list[str]]:
        result = _analyze(self.SOURCES)
        found: dict[str, list[str]] = {}
        for claim in result.claims:
            if claim.produced_by.startswith("java-lexical"):
                found.setdefault(claim.category, []).append(claim.claim)
        return found

    def test_a_supertype_is_reported_with_a_receipt(self) -> None:
        claims = self._claims()
        self.assertTrue(
            any(
                "declares Greeter as a supertype" in item for item in claims["trait_implementation"]
            )
        )

    def test_an_annotated_route_names_its_verb(self) -> None:
        routes = self._claims()["http_route"]
        self.assertTrue(
            any(item.startswith("GET /health is handled by p.Service.health") for item in routes)
        )

    def test_a_route_annotation_without_a_verb_does_not_invent_one(self) -> None:
        # `@RequestMapping` names no method. Reporting GET would be a
        # statement about this reader rather than about the code.
        routes = self._claims()["http_route"]
        matching = next(item for item in routes if "/users" in item)
        self.assertTrue(matching.startswith("/users is handled by"))
        self.assertIn("names no HTTP method", matching)

    def test_a_non_final_static_field_is_reported_and_a_constant_is_not(self) -> None:
        state = self._claims()["process_local_state"]
        self.assertTrue(any("counter" in item for item in state))
        self.assertFalse(any("LIMIT" in item for item in state))

    def test_an_interface_member_counts_as_public_without_the_modifier(self) -> None:
        # Interface members are implicitly public, so counting explicit
        # `public` reported every interface as exposing nothing.
        surface = self._claims()["public_api"]
        matching = next(item for item in surface if item.startswith("p.Greeter"))
        self.assertIn("exposing 1 public member(s)", matching)

    def test_the_entry_point_is_named(self) -> None:
        self.assertTrue(
            any(
                "main is a program entry point" in item
                for item in self._claims()["application_entry"]
            )
        )

    def test_every_claim_carries_a_receipt(self) -> None:
        result = _analyze(self.SOURCES)
        evidence_ids = {item.evidence_id for item in result.evidence}
        for claim in result.claims:
            if claim.produced_by.startswith("java-lexical"):
                self.assertTrue(claim.supporting_evidence, claim.claim)
                self.assertTrue(set(claim.supporting_evidence) <= evidence_ids)

    def test_coverage_reports_every_eligible_file(self) -> None:
        result = _analyze(self.SOURCES)
        coverage = next(item for item in result.coverage if item.language == "Java")
        self.assertEqual(coverage.eligible_files, 2)
        self.assertEqual(coverage.analyzed_files, 2)
        self.assertEqual(coverage.failed_files, 0)


class SymbolIdentityTests(TestCase):
    """A name that repeats in one file is more than one symbol.

    `java.util.stream.ReduceOps` declares a local class called `ReducingSink`
    twelve times, once inside each method. Keying identity on the name alone
    collapsed all twelve into a single ledger row, and twenty-four symbols
    disappeared across `java.base` with nothing reporting it.
    """

    SOURCE = """package p;
class ReduceOps {
    static void first() { class Sink {} }
    static void second() { class Sink {} }
    static void third() { class Sink {} }
}
"""

    def test_each_declaration_keeps_its_own_identity(self) -> None:
        result = _analyze({"ReduceOps.java": self.SOURCE})
        sinks = [item for item in result.symbols if item.qualified_name.endswith(".Sink")]
        self.assertEqual(len(sinks), 3)
        self.assertEqual(len({item.symbol_id for item in sinks}), 3)

    def test_no_symbol_identity_repeats(self) -> None:
        result = _analyze({"ReduceOps.java": self.SOURCE})
        identities = [item.symbol_id for item in result.symbols]
        self.assertEqual(len(identities), len(set(identities)))
