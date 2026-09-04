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
    declared_constants,
    declared_enums,
    declared_members,
    declared_throws,
    declared_types,
    enum_constants,
    environment_reads,
    imported_types,
    package_name,
    record_components,
    throw_sites,
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

    def test_locality_is_inherited_by_a_local_class_member(self) -> None:
        # `JSlider` declares a local class inside a method, and that class
        # declares a member of its own. The member sits correctly in its
        # owner's body and is reachable by no qualified name, because its
        # owner is not. Judging each type only against its immediate owner
        # published `JSlider.SmartHashtable.LabelUIResource` -- the single
        # disagreement across twelve thousand files of the JDK.
        source = (
            "class Outer { void go() { class Local { class Inner {} } } "
            "static class Member { class Deep {} } }"
        )
        found = {item.name: item.local for item in declared_types(tokenize(source))}
        self.assertTrue(found["Outer.Local"])
        self.assertTrue(found["Outer.Local.Inner"])
        self.assertFalse(found["Outer.Member"])
        self.assertFalse(found["Outer.Member.Deep"])

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

    def test_a_qualified_supertype_is_one_name(self) -> None:
        # `implements java.security.PrivilegedAction` names one supertype.
        # Reading each dotted segment as its own produced three, two of them
        # packages that nothing can implement -- fabricated supertypes that
        # reached the specification as verified claims with receipts, and
        # added `implements java` edges for capability clustering to reason
        # over. The declaration differential cannot see this: it compares
        # type declarations, never their supertypes.
        source = "public class A implements java.security.PrivilegedAction<Boolean> {}"
        self.assertEqual(
            declared_types(tokenize(source))[0].supertypes,
            ("java.security.PrivilegedAction",),
        )

    def test_qualified_and_bare_supertypes_mix(self) -> None:
        source = "class B extends java.util.AbstractList<String> implements java.io.Serializable, Runnable {}"
        self.assertEqual(
            declared_types(tokenize(source))[0].supertypes,
            ("java.util.AbstractList", "java.io.Serializable", "Runnable"),
        )

    def test_a_nested_supertype_keeps_its_owner(self) -> None:
        source = "class E implements Map.Entry<K, V> {}"
        self.assertEqual(declared_types(tokenize(source))[0].supertypes, ("Map.Entry",))

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


class JavaContractRoleTests(TestCase):
    SOURCE = "public class Service implements Runnable { public void run() {} }\n"

    def _categories(self, path: str) -> set[str]:
        result = _analyze({path: self.SOURCE})
        return {
            claim.category
            for claim in result.claims
            if claim.produced_by.startswith("java-lexical")
        }

    def test_product_source_publishes_its_surface_and_contracts(self) -> None:
        found = self._categories("src/Service.java")
        self.assertIn("public_api", found)
        self.assertIn("trait_implementation", found)

    def test_test_and_example_types_are_not_product_contracts(self) -> None:
        for path in ("tests/ServiceTest.java", "examples/Service.java"):
            with self.subTest(path=path):
                found = self._categories(path)
                self.assertNotIn("public_api", found)
                self.assertNotIn("trait_implementation", found)


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


class EnumAndRecordSurfaceTests(TestCase):
    """The parts of an enum and a record that callers actually name.

    Both declare their public surface outside the member body: an enum's
    constants come before the first `;`, a record's components sit in its
    header. Reading only the body reported `public enum Color` as exposing
    one member and `public record Point(int x, int y)` as exposing one, when
    the constants and the components are the entire point of each.

    Worse, a constant carrying arguments -- `RED("r")` -- has the shape of a
    method declaration, so it was reported as a method named `RED` and
    consumed the rest of the list, losing `GREEN` and `BLUE` outright.
    `javac -Xprint` confirms all of these: it prints `public int x();` for a
    component and lists every constant.
    """

    ENUM = """\
package p;
public enum Color {
    RED("r"), GREEN, BLUE;
    public String label() { return ""; }
}
"""
    RECORD = """\
package p;
public record Point(int x, int y) {
    public double distance() { return 0; }
}
"""

    def test_every_constant_is_found_including_one_with_arguments(self) -> None:
        found = enum_constants(tokenize(self.ENUM))
        self.assertEqual([name for _, name, _ in found], ["RED", "GREEN", "BLUE"])

    def test_a_constant_body_does_not_swallow_the_rest_of_the_list(self) -> None:
        source = "enum E { A { void go() {} }, B, C; }"
        self.assertEqual([name for _, name, _ in enum_constants(tokenize(source))], ["A", "B", "C"])

    def test_members_after_the_semicolon_are_not_constants(self) -> None:
        found = enum_constants(tokenize(self.ENUM))
        self.assertNotIn("label", [name for _, name, _ in found])

    def test_record_components_are_read_from_the_header(self) -> None:
        found = record_components(tokenize(self.RECORD))
        self.assertEqual([name for _, name, _ in found], ["x", "y"])

    def test_a_generic_record_still_yields_its_components(self) -> None:
        source = "package p;\npublic record Pair<A, B>(A left, java.util.List<B> right) {}\n"
        found = record_components(tokenize(source))
        self.assertEqual([name for _, name, _ in found], ["left", "right"])

    def test_a_class_is_not_mistaken_for_a_record(self) -> None:
        self.assertEqual(record_components(tokenize("class A { void go(int x) {} }")), [])

    def test_the_enum_surface_counts_its_constants(self) -> None:
        result = _analyze({"Color.java": self.ENUM})
        claim = next(item for item in result.claims if item.category == "public_api")
        self.assertIn("exposing 4 public member(s)", claim.claim)

    def test_the_record_surface_counts_its_components(self) -> None:
        result = _analyze({"Point.java": self.RECORD})
        claim = next(item for item in result.claims if item.category == "public_api")
        self.assertIn("exposing 3 public member(s)", claim.claim)

    def test_a_constant_with_arguments_is_not_reported_as_a_method(self) -> None:
        result = _analyze({"Color.java": self.ENUM})
        methods = [item for item in result.symbols if item.qualified_name.endswith(".RED")]
        self.assertEqual(methods, [])


class JavaDeclaredValueTests(TestCase):
    """Java states a vocabulary the way Rust does, and none of it was read.

    Python states a closed set with a frozenset, TypeScript with a union of
    literals, Rust with an enum; all three are recorded. Java states one the
    same way Rust does, and `java.util.concurrent` could declare every unit of
    time it understands without a specification naming one.
    """

    def test_enum_constants_are_a_vocabulary(self) -> None:
        found = declared_enums(
            tokenize(
                "public enum TimeUnit {\n"
                "    NANOSECONDS(TimeUnit.NANO_SCALE),\n"
                "    DAYS(TimeUnit.DAY_SCALE);\n"
                "    private static final long NANO_SCALE = 1L;\n"
                "}\n"
            )
        )
        self.assertEqual(found["TimeUnit"]["members"], ["NANOSECONDS", "DAYS"])

    def test_constants_stop_at_the_semicolon_that_ends_them(self) -> None:
        # Fields and methods follow the constants in an enum body. Reading
        # past the `;` would report a field name as a member of the set.
        found = declared_enums(tokenize("enum E { A, B; static final int X = 1; void run() {} }\n"))
        self.assertEqual(found["E"]["members"], ["A", "B"])

    def test_a_supertype_list_does_not_hide_the_body(self) -> None:
        found = declared_enums(tokenize("enum Simple implements Runnable { A, B }\n"))
        self.assertEqual(found["Simple"]["members"], ["A", "B"])

    def test_one_constant_is_not_a_vocabulary(self) -> None:
        self.assertEqual(declared_enums(tokenize("enum One { ONLY }\n")), {})

    def test_a_static_final_literal_is_a_tunable(self) -> None:
        found = declared_constants(tokenize("class P { static final int MAX_CAP = 32767; }\n"))
        self.assertEqual(found["MAX_CAP"]["value"], "32767")

    def test_a_computed_value_is_not_a_literal(self) -> None:
        # `Integer.SIZE - 3` has no literal value to report, and naming the
        # first token of the expression would state a number the program
        # never uses.
        self.assertEqual(
            declared_constants(tokenize("class P { static final int BITS = Integer.SIZE - 3; }\n")),
            {},
        )

    def test_a_string_constant_keeps_its_value(self) -> None:
        found = declared_constants(tokenize('class P { static final String NAME = "pool"; }\n'))
        self.assertEqual(found["NAME"]["value"], "pool")


class JavaFailureSurfaceTests(TestCase):
    """What a file throws, and what its signatures say it may throw.

    Read out of `java.util.concurrent`, where 551 of 657 throws are `throw new
    X(...)`, 13 re-raise a caught variable, and `BlockingQueue` declares
    `InterruptedException` on six methods while containing no `throw` at all.
    """

    def test_a_thrown_type_is_named(self) -> None:
        found = throw_sites(tokenize("class P { void f() { throw new IllegalStateException(); } }"))
        self.assertEqual(
            [(name, message) for name, message, _ in found], [("IllegalStateException", None)]
        )

    def test_a_literal_message_is_quoted(self) -> None:
        found = throw_sites(
            tokenize('class P { void f() { throw new IllegalArgumentException("Queue full"); } }')
        )
        self.assertEqual(found[0][1], "Queue full")

    def test_a_built_message_is_not_quoted(self) -> None:
        # `"bad " + name` has no fixed text, and quoting its first half would
        # give a reader words to search for that the program never prints.
        found = throw_sites(
            tokenize('class P { void f() { throw new IllegalArgumentException("bad " + n); } }')
        )
        self.assertEqual(found[0], ("IllegalArgumentException", None, 1))

    def test_a_rethrow_names_no_type(self) -> None:
        found = throw_sites(tokenize("class P { void f() { throw ex; } }"))
        self.assertEqual(found, [(None, None, 1)])

    def test_a_qualified_name_is_the_same_type_as_its_import(self) -> None:
        # `ArrayBlockingQueue` writes both spellings three methods apart.
        found = declared_throws(
            tokenize(
                "class P {\n"
                "  void a() throws IOException {}\n"
                "  void b() throws java.io.IOException {}\n"
                "}\n"
            )
        )
        self.assertEqual(sorted(found), ["IOException"])
        self.assertEqual(found["IOException"]["count"], 2)

    def test_a_throws_clause_lists_every_type(self) -> None:
        found = declared_throws(
            tokenize("interface Q { void take() throws InterruptedException, TimeoutException; }")
        )
        self.assertEqual(sorted(found), ["InterruptedException", "TimeoutException"])

    def test_javadoc_throws_is_not_a_declaration(self) -> None:
        # `@throws` outnumbers the real clause three to one in that package.
        # The tokenizer drops comments, so what is counted is the compiler's
        # copy rather than the prose beside it.
        found = declared_throws(
            tokenize("/** @throws NullPointerException if null */\nclass P { void f() {} }\n")
        )
        self.assertEqual(found, {})

    def test_a_file_that_only_declares_still_reports_a_failure_surface(self) -> None:
        result = _analyze(
            {"Q.java": "public interface Q { void take() throws InterruptedException; }\n"}
        )
        claims = [item for item in result.claims if item.category == "failure_surface"]
        self.assertEqual(len(claims), 1)
        self.assertIn("InterruptedException", claims[0].claim)
        self.assertIn("throws", claims[0].claim)

    def test_a_thrown_file_reports_count_and_types(self) -> None:
        result = _analyze(
            {
                "P.java": (
                    "public class P {\n"
                    '  void a() { throw new IllegalStateException("no"); }\n'
                    "  void b() { throw new IllegalStateException(); }\n"
                    "  void c() { throw new NullPointerException(); }\n"
                    "}\n"
                )
            }
        )
        claims = [item for item in result.claims if item.category == "failure_surface"]
        self.assertEqual(len(claims), 1)
        self.assertIn("throws in 3 place(s), of 2 distinct type(s)", claims[0].claim)
        self.assertIn('"no"', claims[0].claim)

    def test_a_file_that_cannot_fail_claims_nothing(self) -> None:
        result = _analyze({"P.java": "public class P { int f() { return 1; } }\n"})
        self.assertEqual([item for item in result.claims if item.category == "failure_surface"], [])


class JavaEnvironmentTests(TestCase):
    """What a file needs supplied before it will run.

    `System.getProperty` outnumbers `System.getenv` fifty to nine across the
    1,600 files of `java.base`, and every real `getenv` call there passes a
    variable rather than a literal. A reader written for `getenv` alone would
    have found nothing in the standard library.
    """

    def test_a_system_property_is_recorded(self) -> None:
        found = environment_reads(
            tokenize('class P { void f() { System.getProperty("jdk.debug"); } }')
        )
        self.assertEqual(found, [("jdk.debug", "system property", 1)])

    def test_an_environment_setting_is_named_differently(self) -> None:
        found = environment_reads(
            tokenize('class P { void f() { System.getenv("SERVICE_URL"); } }')
        )
        self.assertEqual(found, [("SERVICE_URL", "environment setting", 1)])

    def test_a_property_object_is_not_the_system(self) -> None:
        # `Properties.getProperty` reads a file the program loaded, not
        # something the machine supplies.
        self.assertEqual(
            environment_reads(tokenize('class P { void f() { props.getProperty("a.b"); } }')), []
        )

    def test_a_name_held_in_a_variable_is_not_recorded(self) -> None:
        # Every `System.getenv` in `java.base` is this shape.
        self.assertEqual(
            environment_reads(tokenize("class P { void f() { System.getenv(name); } }")), []
        )

    def test_a_javadoc_example_is_not_a_read(self) -> None:
        # `System.java` documents `System.getenv("FOO").equals(...)` in prose
        # and `InputStreamReader` shows `getProperty("stdin.encoding")` in a
        # `{@snippet}`. A grep counts all three; the tokenizer drops comments.
        found = environment_reads(
            tokenize('/** {@code System.getenv("FOO")} */\nclass P { void f() {} }\n')
        )
        self.assertEqual(found, [])

    def test_a_read_becomes_a_claim(self) -> None:
        result = _analyze(
            {"P.java": 'public class P { void f() { System.getenv("SERVICE_URL"); } }\n'}
        )
        claims = [item for item in result.claims if item.category == "configuration_read"]
        self.assertEqual(len(claims), 1)
        self.assertIn("SERVICE_URL", claims[0].claim)
