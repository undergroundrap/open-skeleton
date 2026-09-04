# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.typescript_lexical import (
    CatchHandler,
    TypeScriptLexicalAnalyzer,
    _call_sites,
    _catch_handlers,
    _declarations,
    _environment_reads,
    _exported_names,
    _external_origins,
    _imported_names,
    _module_names,
    _module_state,
    _object_keys,
    _parameter_names,
    _references,
    _throw_messages,
    _throw_sites,
    _tokens,
    _tunables,
    _value_constants,
)
from open_skeleton.scanner import scan_repository

SOURCE = """\
import React, { useEffect, useState } from "react";

export default function App() {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    fetch("http://localhost:8000/ready").then(() => setReady(true));
    localStorage.setItem("ready", String(ready));
  }, [ready]);
  return <main>{String(ready)}</main>;
}
// fetch("http://localhost:8000/commented")
const label = "fetch(http://localhost:8000/not-a-call)";
"""


class TypeScriptAnalyzerTests(TestCase):
    def test_tokenizer_ignores_comments_and_does_not_treat_strings_as_calls(self) -> None:
        tokens = _tokens(SOURCE)
        fetch_calls = sum(
            token.value == "fetch" and index + 1 < len(tokens) and tokens[index + 1].value == "("
            for index, token in enumerate(tokens)
        )
        self.assertEqual(fetch_calls, 1)

    def test_emits_symbols_edges_and_receipted_client_findings(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.tsx").write_text(SOURCE, encoding="utf-8")
            result = TypeScriptLexicalAnalyzer().analyze(scan_repository(root))
            claims = {claim.category: claim for claim in result.claims}

            self.assertEqual(result.coverage[0].coverage_ratio, 1.0)
            self.assertTrue(any(symbol.qualified_name == "app.App" for symbol in result.symbols))
            self.assertTrue(any(edge.relationship == "imports" for edge in result.edges))
            self.assertEqual(
                claims["http_client_inventory"].claim,
                "app.tsx contains 1 fetch call sites.",
            )
            self.assertEqual(
                claims["hardcoded_endpoint"].claim,
                "app.tsx contains 2 string-literal references to http://localhost:8000.",
            )
            evidence_ids = {receipt.evidence_id for receipt in result.evidence}
            for claim in result.claims:
                self.assertTrue(set(claim.supporting_evidence).issubset(evidence_ids))
            self.assertTrue(all(receipt.excerpt_sha256 for receipt in result.evidence))

    def test_counts_typescript_generic_react_hook_calls(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "page.tsx").write_text(
                """\
import { useRef, useState } from 'react';
const plain = useState(null);
const typed = useState<string | null>(null);
const nested = useRef<Map<string, Array<number>>>(new Map());
""",
                encoding="utf-8",
            )

            result = TypeScriptLexicalAnalyzer().analyze(scan_repository(root))
            state_claims = {item.claim for item in result.claims if item.category == "ui_state"}

            self.assertIn("page.tsx calls React hook useState 2 times.", state_claims)
            self.assertIn("page.tsx calls React hook useRef 1 times.", state_claims)


class StateValueDomainTests(TestCase):
    def _fields(self, source: str) -> dict[str, Any]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "page.tsx").write_text(source, encoding="utf-8")
            result = TypeScriptLexicalAnalyzer().analyze(scan_repository(root))
        for symbol in result.symbols:
            fields = symbol.metadata.get("state_fields")
            if fields:
                return dict(fields)
        return {}

    def test_strict_equality_comparisons_form_a_domain(self) -> None:
        fields = self._fields('if (step === "intro") { a(); }\nif (step === "game") { b(); }\n')
        self.assertEqual(fields["step"]["values"], ["game", "intro"])

    def test_assignments_are_recorded_as_entries(self) -> None:
        fields = self._fields('let mode = "light";\nmode = "dark";\n')
        values = {value for value, _, _ in fields["mode"]["entries"]}
        self.assertEqual(values, {"light", "dark"})

    def test_a_single_value_is_not_a_domain(self) -> None:
        self.assertEqual(self._fields('let only = "one";\n'), {})

    def test_a_literal_in_a_comment_is_not_counted(self) -> None:
        # The tokenizer drops comments, so the domain must not see them.
        self.assertEqual(self._fields('// step === "ghost"\nlet x = "a";\n'), {})

    def test_a_long_literal_is_not_treated_as_a_state_value(self) -> None:
        long_value = "x" * 60
        self.assertEqual(self._fields(f'let m = "a";\nm = "{long_value}";\n'), {})


class TypeScriptDeclarationTests(TestCase):
    """Most modern TypeScript declares through bindings, not declaration keywords.

    A module whose every function is `const handleX = () => {}` reported no
    symbols at all before these cases existed.
    """

    def _declared(self, source: str) -> dict[str, str]:
        return {item.name: item.kind for item in _declarations(_tokens(source))}

    def test_an_arrow_binding_is_a_function_and_a_literal_binding_is_a_constant(self) -> None:
        declared = self._declared(
            "export const addLog = (line: string) => { push(line); };\n"
            "export const ATTACK_COOLDOWN_MS = 1500;\n"
        )
        self.assertEqual(declared["addLog"], "function")
        self.assertEqual(declared["ATTACK_COOLDOWN_MS"], "constant")

    def test_a_type_annotation_does_not_hide_the_initializer(self) -> None:
        self.assertEqual(self._declared('const label: string = "hi";\n')["label"], "constant")

    def test_an_arrow_inside_a_call_does_not_make_the_binding_a_function(self) -> None:
        # `useMemo(() => compute(), [])` holds a value. Only an arrow at the
        # initializer's own depth means the name itself is callable.
        self.assertEqual(
            self._declared("const rows = useMemo(() => compute(), []);\n")["rows"], "binding"
        )

    def test_destructuring_binds_the_renamed_name_not_the_lookup_key(self) -> None:
        declared = self._declared("const { rows, total: count } = props;\nconst [head] = list;\n")
        self.assertIn("rows", declared)
        self.assertIn("count", declared)
        self.assertIn("head", declared)
        self.assertNotIn("total", declared)

    def test_class_and_interface_members_are_qualified_by_their_container(self) -> None:
        declared = self._declared(
            "export interface PlayerState { hp: number; mana?: number; act(): void; }\n"
            "export class Engine {\n"
            "  private static readonly limit = 5;\n"
            "  async resolveTick(dt: number) { return dt; }\n"
            "}\n"
        )
        self.assertEqual(declared["PlayerState.hp"], "property")
        self.assertEqual(declared["PlayerState.act"], "method")
        self.assertEqual(declared["Engine.limit"], "property")
        self.assertEqual(declared["Engine.resolveTick"], "method")

    def test_a_parameter_is_not_a_member_of_the_enclosing_class(self) -> None:
        # Parameters sit inside parentheses at the same brace depth as members,
        # so without paren tracking `dt` and `db` were recorded as class fields.
        declared = self._declared(
            "class Engine {\n"
            "  constructor(private db: DB) {}\n"
            "  resolveTick(dt: number) { return dt; }\n"
            "}\n"
        )
        self.assertNotIn("Engine.dt", declared)
        self.assertNotIn("Engine.db", declared)

    def test_a_function_local_is_not_dressed_up_as_a_class_member(self) -> None:
        declared = self._declared("class Engine {\n  run() { const inner = 1; }\n}\n")
        self.assertEqual(declared["inner"], "local")
        self.assertNotIn("Engine.inner", declared)

    def test_enum_members_are_recorded(self) -> None:
        declared = self._declared("enum Slot { Head, Chest = 2 }\n")
        self.assertEqual(declared["Slot.Head"], "enum_member")
        self.assertEqual(declared["Slot.Chest"], "enum_member")

    def test_a_declaration_inside_a_comment_or_string_is_not_recorded(self) -> None:
        declared = self._declared('// const ghost = 1;\nconst real = "const phantom = 2;";\n')
        self.assertIn("real", declared)
        self.assertNotIn("ghost", declared)
        self.assertNotIn("phantom", declared)


class TypeScriptReferenceTests(TestCase):
    """What a module reaches for but does not own."""

    def _refs(self, source: str) -> dict[str, Any]:
        tokens = _tokens(source)
        declared = frozenset(
            {item.name.rsplit(".", 1)[-1] for item in _declarations(tokens)}
            | _parameter_names(tokens)
        )
        return _references(tokens, declared)

    def test_a_platform_call_is_recorded_with_its_use(self) -> None:
        refs = self._refs("localStorage.setItem('k', '1');")
        self.assertTrue(refs["localStorage.setItem"]["called"])

    def test_a_callback_parameter_is_not_an_external_reference(self) -> None:
        # `m` is bound by the arrow, so `m.respawn_at` is local field access
        # rather than a platform API this module depends on.
        self.assertNotIn("m.respawn_at", self._refs("mobs.map(m => m.respawn_at);"))

    def test_a_parenthesised_arrow_parameter_is_also_excluded(self) -> None:
        self.assertNotIn("a.id", self._refs("rows.map((a, i) => a.id);"))

    def test_a_name_this_module_declares_is_not_external(self) -> None:
        self.assertNotIn("engine.run", self._refs("const engine = build();engine.run();"))

    def test_a_constructed_global_is_recorded(self) -> None:
        self.assertIn("AbortController", self._refs("const c = new AbortController();"))


class TypeScriptImportTests(TestCase):
    def test_default_and_named_imports_are_both_recorded(self) -> None:
        imports = _imported_names(_tokens('import React, { useState } from "react";'))
        self.assertEqual(imports["react"]["names"], ["React", "useState"])

    def test_an_alias_is_recorded_under_the_local_name(self) -> None:
        imports = _imported_names(_tokens('import { createPortal as portal } from "react-dom";'))
        self.assertEqual(imports["react-dom"]["names"], ["portal"])

    def test_a_relative_module_keeps_its_specifier(self) -> None:
        imports = _imported_names(_tokens('import styles from "./page.module.css";'))
        self.assertEqual(imports["./page.module.css"]["names"], ["styles"])


class ExternalOriginTests(TestCase):
    """A host in a string literal is a data-egress decision made in source."""

    def test_a_third_party_host_is_recorded(self) -> None:
        origins = _external_origins(_tokens('const f = "https://fonts.googleapis.com/css2?x=1";'))
        self.assertEqual(origins["fonts.googleapis.com"]["scheme"], "https")

    def test_a_loopback_host_is_not_a_third_party(self) -> None:
        self.assertEqual(_external_origins(_tokens('const a = "http://localhost:8000/api";')), {})
        self.assertEqual(_external_origins(_tokens('const a = "http://127.0.0.1:9/x";')), {})

    def test_a_websocket_origin_is_recorded(self) -> None:
        self.assertIn(
            "stream.example.com",
            _external_origins(_tokens('let s = "wss://stream.example.com/v1";')),
        )

    def test_a_string_that_merely_contains_a_url_is_not_an_origin(self) -> None:
        # The pattern is anchored, so prose mentioning a URL is not a fetch.
        self.assertEqual(
            _external_origins(_tokens('const t = "see https://example.com for docs";')), {}
        )

    def test_a_bare_global_call_is_an_external_reference(self) -> None:
        tokens = _tokens("setTimeout(run, 500);")
        declared = frozenset(
            {item.name.rsplit(".", 1)[-1] for item in _declarations(tokens)}
            | _parameter_names(tokens)
        )
        self.assertTrue(_references(tokens, declared)["setTimeout"]["called"])

    def test_control_flow_is_not_mistaken_for_a_call(self) -> None:
        tokens = _tokens("if (ready) { while (x) { doThing(); } }")
        refs = _references(tokens, frozenset())
        self.assertNotIn("if", refs)
        self.assertNotIn("while", refs)


class ObjectKeyTests(TestCase):
    """A payload assembled inline is a contract that exists only as keys."""

    def _keys(self, source: str) -> dict[str, Any]:
        tokens = _tokens(source)
        declared = frozenset(item.name.rsplit(".", 1)[-1] for item in _declarations(tokens))
        return _object_keys(tokens, declared)

    def test_literal_keys_are_recorded(self) -> None:
        keys = self._keys("const body = { player_name: n, target_hp: h };")
        self.assertEqual(sorted(keys), ["player_name", "target_hp"])

    def test_a_statement_block_is_not_an_object_literal(self) -> None:
        # `{` after `)` opens a block. Its contents are statements, and
        # treating them as keys filled the panel with control flow.
        self.assertEqual(self._keys("function f(a) { if (a) { return 1; } }"), {})

    def test_a_nested_object_contributes_its_keys(self) -> None:
        self.assertIn("inner_field", self._keys("const o = { outer: { inner_field: 2 } };"))

    def test_a_member_access_is_not_a_key(self) -> None:
        self.assertNotIn("hp", self._keys("const o = { total: player.hp };"))

    def test_shorthand_counts_only_for_a_name_this_module_declares(self) -> None:
        declared = self._keys("const hp = 3; const o = { hp };")
        self.assertIn("hp", declared)
        self.assertEqual(self._keys("const o = { unknownName };"), {})


class TypeScriptClaimFamilyTests(TestCase):
    """The same facts Python and Rust report, named the same way."""

    def _state(self, source: str) -> list[Any]:
        tokens = _tokens(source)
        return _module_state(tokens, _declarations(tokens))

    def test_a_mutated_module_container_is_process_local_state(self) -> None:
        found = self._state("const cache = new Map();\nexport function put(k){ cache.set(k, 1); }")
        self.assertEqual([name for name, _ in found], ["cache"])

    def test_an_array_written_by_push_counts(self) -> None:
        found = self._state("const queue = [];\nfunction add(v){ queue.push(v); }")
        self.assertEqual([name for name, _ in found], ["queue"])

    def test_a_container_that_is_never_mutated_is_a_lookup_table(self) -> None:
        # Calling a constant table state would repeat an error this codebase
        # has already made twice.
        self.assertEqual(self._state("const lookup = { a: 1 };\nconst x = lookup.a;"), [])

    def test_a_subscript_read_is_not_a_write(self) -> None:
        self.assertEqual(self._state("const m = {};\nconst v = m[key];"), [])

    def test_a_subscript_assignment_is_a_write(self) -> None:
        found = self._state("const m = {};\nfunction set(k, v){ m[k] = v; }")
        self.assertEqual([name for name, _ in found], ["m"])

    def test_environment_reads_cover_both_access_forms(self) -> None:
        found = _environment_reads(
            _tokens('const a = process.env.API_KEY;\nconst b = process.env["REGION"];')
        )
        self.assertEqual(sorted(found), ["API_KEY", "REGION"])

    def test_an_unrelated_env_property_is_not_a_setting(self) -> None:
        self.assertEqual(_environment_reads(_tokens("const e = config.env.mode;")), {})

    def test_thrown_types_are_recorded(self) -> None:
        found = _throw_sites(
            _tokens(
                'function f(){ throw new ConfigError("x"); }\nfunction g(){ throw new TypeError("y"); }'
            )
        )
        self.assertEqual(sorted(found), ["ConfigError", "TypeError"])


class TunableTests(TestCase):
    """Named literal constants, including inside an IIFE wrapper.

    Found by running against a physics-heavy browser game. Its 16 modules
    produced 4 claims while the constants deciding how the whole thing behaves
    -- gravity, friction, acceleration caps -- sat in plain sight in every file
    and were recorded nowhere, because the Rust analyzer had carried tunables
    since it was written and this one never did.
    """

    def test_a_module_constant_is_recorded_with_its_value(self) -> None:
        found = _tunables(_tokens("const GRAVITY = 16.0;\n"))
        self.assertEqual(found["GRAVITY"]["value"], "16.0")

    def test_a_constant_inside_an_iife_is_recorded(self) -> None:
        # Buildless browser code wraps everything one level deep. Requiring
        # depth zero finds nothing in an entire category of real JavaScript.
        source = "(function (global) {\n  const AIR_ACCEL = 12.0;\n})(window);\n"
        self.assertIn("AIR_ACCEL", _tunables(_tokens(source)))

    def test_a_negative_constant_keeps_its_sign(self) -> None:
        self.assertEqual(_tunables(_tokens("const FLOOR = -9.8;\n"))["FLOOR"]["value"], "-9.8")

    def test_a_string_constant_is_recorded_as_a_value_not_a_dial(self) -> None:
        # Strings used to arrive in the tunable index and render under the
        # heading "Numeric tunables", which put `MODE = "surf"` in a table of
        # numbers a maintainer would retune.
        self.assertEqual(_tunables(_tokens('const MODE = "surf";\n')), {})
        found = _value_constants(_tokens('const MODE = "surf";\n'))
        self.assertEqual(found["MODE"]["value"], "surf")

    def test_a_function_local_is_not_a_tunable(self) -> None:
        # Depth is what separates a knob from a scratch variable.
        source = "function step() {\n  if (true) {\n    const scratch = 3;\n  }\n}\n"
        self.assertEqual(_tunables(_tokens(source)), {})

    def test_a_computed_initializer_is_not_recorded(self) -> None:
        # `const X = a * b` has no literal value to report without evaluating it.
        self.assertEqual(_tunables(_tokens("const SPEED = base * 2;\n")), {})


class ExportSurfaceTests(TestCase):
    """Which names a module makes public, which `_declarations` cannot say.

    Found by running against a date library of 1,579 modules that produced
    0.07 claims per file. That was not a quiet codebase: every module exports
    the one function it exists to provide, and an internal helper was recorded
    identically to it. In an ES module the keyword is the declaration, and
    `export` is more explicit than the `__all__` list it corresponds to.
    """

    def _claim_categories(self, path: str) -> set[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("export function run() {}\n", encoding="utf-8")
            result = TypeScriptLexicalAnalyzer().analyze(scan_repository(root))
        return {claim.category for claim in result.claims}

    def test_an_exported_function_is_public_and_a_helper_is_not(self) -> None:
        found = _exported_names(_tokens("export function addDays(d, n) {}\nfunction helper() {}\n"))
        self.assertEqual(found, ["addDays"])

    def test_an_export_list_records_each_name(self) -> None:
        self.assertEqual(_exported_names(_tokens("export { a, b };\n")), ["a", "b"])

    def test_a_renamed_export_records_the_public_name(self) -> None:
        # `export { internal as publicName }` commits to publicName only.
        self.assertEqual(
            _exported_names(_tokens("export { internal as publicName };\n")), ["publicName"]
        )

    def test_a_default_export_binds_default_not_the_local_name(self) -> None:
        # This asserted ["Foo"] until esbuild was asked the same question and
        # answered "default". An importer writes `import Anything from "./x"`,
        # so renaming the class breaks nobody, and reporting `Foo` as the
        # public name asserted a compatibility promise the module never made.
        self.assertEqual(_exported_names(_tokens("export default class Foo {}\n")), ["default"])

    def test_a_star_reexport_claims_no_names(self) -> None:
        # The names live in another file. Listing them here would attribute a
        # surface to the wrong module.
        self.assertEqual(_exported_names(_tokens('export * from "./other";\n')), [])

    def test_types_and_constants_count_as_surface(self) -> None:
        found = _exported_names(_tokens("export const X = 1;\nexport interface Opts {}\n"))
        self.assertEqual(found, ["X", "Opts"])

    def test_an_exported_product_module_publishes_a_contract(self) -> None:
        self.assertIn("public_api", self._claim_categories("src/api.ts"))

    def test_a_test_or_example_export_is_not_the_product_contract(self) -> None:
        for path in ("tests/api.test.ts", "examples/api.ts"):
            with self.subTest(path=path):
                self.assertNotIn("public_api", self._claim_categories(path))


class ExportAndTunableEdgeTests(TestCase):
    """Cases found by reading this code rather than by running it on a repo.

    Every defect this session was found by pointing the engine at unseen code,
    which meant the shapes no repository happened to contain went unexamined.
    These are three of those, and two produced fabricated facts -- an export
    named `type` that no module has, and a scratch variable inside a callback
    reported as a knob a maintainer would tune.
    """

    def test_a_type_only_export_list_is_a_surface(self) -> None:
        # `export type { Foo }` is how a TypeScript package publishes its types.
        self.assertEqual(_exported_names(_tokens("export type { Foo, Bar };")), ["Foo", "Bar"])

    def test_an_inline_type_modifier_is_not_an_exported_name(self) -> None:
        # `export { type Foo, Bar }` once reported an export called `type`.
        self.assertEqual(_exported_names(_tokens("export { type Foo, Bar };")), ["Foo", "Bar"])

    def test_a_constant_inside_a_callback_is_not_a_module_tunable(self) -> None:
        source = "const OUT = 1;\nconst f = () => { const scratch = 999; };\n"
        self.assertEqual(list(_tunables(_tokens(source))), ["OUT"])

    def test_a_constant_inside_an_assigned_function_expression_is_not_a_tunable(self) -> None:
        self.assertEqual(_tunables(_tokens("const f = function () { const s = 9; };")), {})

    def test_a_nested_function_inside_an_iife_does_not_contribute_tunables(self) -> None:
        # The IIFE body holds module constants; a function inside it holds locals.
        source = "(function (g) {\n  const OK = 1;\n  function h() { const bad = 2; }\n})(w);\n"
        self.assertEqual(list(_tunables(_tokens(source))), ["OK"])

    def test_the_iife_wrapper_still_yields_its_constants(self) -> None:
        source = "(function (global) {\n  const GRAVITY = 16.0;\n})(window);\n"
        self.assertEqual(list(_tunables(_tokens(source))), ["GRAVITY"])


class RegexLiteralTests(TestCase):
    r"""The JavaScript lexing trap, equivalent to the three Rust ones.

    A regex may contain a quote. Without a concept of regex literals the
    tokenizer read `/^[^\s@"]+$/` as opening a string and swallowed everything
    to the next quote -- so every declaration after the first such regex
    vanished, along with every claim that rested on them. One file in `zod`
    lost five exports this way, and nothing in the suite noticed because no
    fixture contained a regex with a quote in it.

    Deciding whether `/` divides or matches cannot be done from the slash. It
    is settled by what precedes it: a value can be divided, anything else
    cannot.
    """

    def test_a_regex_containing_a_quote_does_not_swallow_the_file(self) -> None:
        found = _exported_names(_tokens('const r = /ab"c/;\nexport const AFTER = 1;\n'))
        self.assertEqual(found, ["AFTER"])

    def test_division_is_not_read_as_a_regex(self) -> None:
        values = [item.value for item in _tokens("const a = b / c / d;")]
        self.assertEqual(values.count("/"), 2)

    def test_a_regex_after_a_keyword_is_consumed(self) -> None:
        values = [item.value for item in _tokens("return /x/.test(y);")]
        self.assertNotIn("x", values)

    def test_division_after_a_closing_parenthesis_is_division(self) -> None:
        values = [item.value for item in _tokens("const x = (a + b) / 2;")]
        self.assertIn("/", values)
        self.assertIn("2", values)

    def test_a_character_class_may_contain_a_slash(self) -> None:
        # `[/]` does not end the pattern, so the literal runs past it.
        found = _exported_names(_tokens("const r = /[/]x/;\nexport const AFTER = 1;\n"))
        self.assertEqual(found, ["AFTER"])


class ExportFormTests(TestCase):
    """Export forms found by comparing against esbuild on a real package."""

    def test_a_namespace_reexport_binds_its_alias(self) -> None:
        # `export * as core from` names something; bare `export * from` does not.
        self.assertEqual(_exported_names(_tokens('export * as core from "./x";')), ["core"])

    def test_a_typescript_namespace_is_a_surface(self) -> None:
        self.assertEqual(_exported_names(_tokens("export namespace errorUtil { }")), ["errorUtil"])

    def test_a_destructured_export_binds_each_name(self) -> None:
        self.assertEqual(_exported_names(_tokens("export const { GET } = make();")), ["GET"])

    def test_a_renamed_destructured_export_binds_the_new_name(self) -> None:
        # `{ a: b }` reads key a and binds b.
        self.assertEqual(_exported_names(_tokens("export const { a: b, c } = x;")), ["b", "c"])

    def test_array_destructuring_binds_each_position(self) -> None:
        self.assertEqual(_exported_names(_tokens("export const [p, q] = y;")), ["p", "q"])


class NestedExportScopeTests(TestCase):
    """Only a top-level `export` names something an importer can ask for.

    The scan took every `export` token in a file regardless of nesting, so
    names declared inside a namespace or a module augmentation were flattened
    into the module's surface. Both are wrong in the same way the Java reader
    was wrong about a class declared inside a method: the name exists, and it
    is not reachable by the name reported.

    Real repositories here were unaffected -- none uses either form -- which
    is exactly why this needed constructing rather than sampling. Library and
    `@types`-style code uses both constantly.
    """

    def test_a_namespace_publishes_itself_not_its_contents(self) -> None:
        # An importer writes `import { N }` and then `N.inner`. Reporting a
        # bare `inner` says `import { inner }` works, and it does not.
        source = "export namespace N { export const inner = 1; }\n"
        self.assertEqual(_exported_names(_tokens(source)), ["N"])

    def test_a_module_augmentation_exports_nothing_from_this_file(self) -> None:
        # `declare module 'x'` adds to a different module entirely.
        source = "declare module 'x' { export const inner: number; }\nexport const outer = 1;\n"
        self.assertEqual(_exported_names(_tokens(source)), ["outer"])

    def test_a_global_augmentation_does_not_contribute_a_name(self) -> None:
        source = "declare global { interface Window { z: number } }\nexport const w = 1;\n"
        self.assertEqual(_exported_names(_tokens(source)), ["w"])

    def test_a_braced_export_list_is_still_read(self) -> None:
        # The list's own braces must not be mistaken for nesting.
        self.assertEqual(_exported_names(_tokens("export {\n a,\n b,\n};\n")), ["a", "b"])

    def test_a_declaration_with_a_body_is_still_exported(self) -> None:
        source = "export class C { m() { return 1; } }\nexport const n = 2;\n"
        self.assertEqual(_exported_names(_tokens(source)), ["C", "n"])

    def test_an_export_after_a_nested_block_is_still_found(self) -> None:
        source = "function outer() { { const deep = 1; } }\nexport const tail = 3;\n"
        self.assertEqual(_exported_names(_tokens(source)), ["tail"])


class ModuleNameCollisionTests(TestCase):
    """Two files do not share one module name.

    The name drops the extension, so a compiled `src/util.js` sitting beside
    its `src/util.ts` source reduced both to `src.util`, and the document
    carried two `src.util exports ...` rows listing different names. That
    reads as a contradiction and is two files.

    Rust hit the same thing an hour earlier through a package holding both
    crate roots, and Python before that through two distributions in one
    workspace. None of the four repositories here collides, which is why this
    is constructed: sampling would have certified it correct.
    """

    def test_a_compiled_sibling_does_not_share_the_source_name(self) -> None:
        found = _module_names(["src/util.ts", "src/util.js"])
        self.assertEqual(found["src/util.ts"], "src.util.ts")
        self.assertEqual(found["src/util.js"], "src.util.js")

    def test_an_index_pair_is_told_apart(self) -> None:
        found = _module_names(["src/api/index.ts", "src/api/index.tsx"])
        self.assertEqual(len(set(found.values())), 2)

    def test_a_name_that_does_not_collide_keeps_its_extension_off(self) -> None:
        # The extension carries nothing a reader needs when it is unambiguous.
        found = _module_names(["a/only.ts", "b/other.js"])
        self.assertEqual(found, {"a/only.ts": "a.only", "b/other.js": "b.other"})

    def test_every_file_resolves_to_a_distinct_name(self) -> None:
        paths = ["p/a.ts", "p/a.js", "p/a.mjs", "p/b.ts"]
        found = _module_names(paths)
        self.assertEqual(len(set(found.values())), len(paths))


class CallSiteTests(TestCase):
    """A call graph with no edges is a graph nothing can traverse.

    This reader emitted `calls` edges for `fetch` and nothing else, so
    capability tracing -- which follows calls out of test and harness files --
    returned nothing for every JavaScript and TypeScript repository regardless
    of how well tested it was. billune produced zero call edges across its
    whole tree and coast-most fourteen.

    The Rust reader had the identical gap and was fixed on its own; the fix
    never crossed. Its docstring even records the symptom, which is what makes
    this worth writing down rather than quietly repairing.
    """

    def _names(self, source: str) -> set[str]:
        return {name for name, _ in _call_sites(_tokens(source))}

    def test_a_plain_call_is_recorded(self) -> None:
        self.assertEqual(self._names("const v = load(id);"), {"load"})

    def test_a_method_call_records_the_method_name(self) -> None:
        # Resolution is lexical: this records `save` without deciding which
        # `save` it is, the same guarantee the rest of the module makes.
        self.assertEqual(self._names("store.save(y);"), {"save"})

    def test_control_flow_taking_a_parenthesis_is_not_a_call(self) -> None:
        source = "if (ready) { while (going) { for (const a of b) { switch (k) {} } } }"
        self.assertEqual(self._names(source), set())

    def test_a_declaration_does_not_call_its_own_name(self) -> None:
        self.assertEqual(self._names("function handler(req) { return 1; }"), set())

    def test_construction_is_not_recorded_as_a_call(self) -> None:
        self.assertEqual(self._names("const w = new Widget(1);"), set())

    def test_a_module_specifier_is_not_a_callee(self) -> None:
        # `import('./m')` and `require('./m')` name a module, not a definition.
        self.assertEqual(self._names("import('./m'); require('./n');"), set())

    def test_a_tagged_template_is_not_a_call(self) -> None:
        self.assertEqual(self._names("const t = tag`x ${y}`;"), set())

    def test_calls_inside_a_declaration_body_are_still_found(self) -> None:
        source = "export default function page() { return render(build()); }"
        self.assertEqual(self._names(source), {"render", "build"})

    def test_each_call_carries_the_line_it_sits_on(self) -> None:
        found = dict(_call_sites(_tokens("a();\n\nb();\n")))
        self.assertEqual(found, {"a": 1, "b": 3})


class ThrownMessageTests(TestCase):
    """A thrown type says a call can fail; the message says how.

    The type was recorded and the message beside it discarded, which is the
    same omission the Python reader had until it was fixed there.
    """

    def _messages(self, source: str) -> dict[str, str]:
        return _throw_messages(_tokens(source))

    def test_a_literal_message_is_quoted_as_written(self) -> None:
        self.assertEqual(
            self._messages('throw new Error("Missing vault");'),
            {"Error": "Missing vault"},
        )

    def test_a_template_literal_that_interpolates_is_not_quoted(self) -> None:
        # The tokenizer strips the delimiters, so the backtick is gone by the
        # time the message is read and `${` is what identifies one. A first
        # attempt tested for the backtick and let `Unknown: ${id}` through.
        self.assertEqual(self._messages("throw new Error(`Unknown: ${id}`);"), {})

    def test_a_template_literal_without_interpolation_is_still_text(self) -> None:
        self.assertEqual(
            self._messages("throw new Error(`plain enough`);"),
            {"Error": "plain enough"},
        )

    def test_a_message_built_from_a_variable_is_not_quoted(self) -> None:
        self.assertEqual(self._messages("throw new Error(problem);"), {})

    def test_rethrowing_a_value_carries_no_message(self) -> None:
        self.assertEqual(self._messages("throw error;"), {})

    def test_the_first_literal_per_type_is_the_one_kept(self) -> None:
        messages = self._messages('throw new Error("first");\nthrow new Error("second");')
        self.assertEqual(messages, {"Error": "first"})

    def test_the_message_reaches_the_failure_surface_claim(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "vault.ts").write_text(
                'export function open() {\n  throw new Error("Missing vault");\n}\n',
                encoding="utf-8",
            )
            result = analyze_snapshot(scan_repository(root))
            claim = next(item for item in result.claims if item.category == "failure_surface")
            self.assertIn('Error ("Missing vault")', claim.claim)


class CatchHandlerTests(TestCase):
    """What a catch block can be said to do, without a parser.

    A first version called a handler "silent" when it neither rethrew nor
    logged. Real interface code answers a failure with `setError("...")`,
    showing the user a message -- a stronger report than a console line -- and
    the rule labelled nine such handlers in one file as continuing silently.
    The claim accused working code of swallowing errors, so the judgement was
    dropped and only what is checkable is asserted.
    """

    def _handlers(self, source: str) -> list[CatchHandler]:
        return _catch_handlers(_tokens(source))

    def test_a_handler_is_found_with_its_binding(self) -> None:
        handlers = self._handlers("try { a(); } catch (problem) { b(); }")
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].binding, "problem")

    def test_an_optional_binding_may_be_absent(self) -> None:
        handlers = self._handlers("try { a(); } catch { b(); }")
        self.assertEqual(len(handlers), 1)
        self.assertIsNone(handlers[0].binding)

    def test_a_rethrow_is_recorded(self) -> None:
        self.assertTrue(self._handlers("try { a(); } catch (e) { throw e; }")[0].rethrows)

    def test_a_body_that_runs_no_statement_is_recorded_as_such(self) -> None:
        self.assertTrue(self._handlers("try { a(); } catch { }")[0].empty)

    def test_a_comment_only_body_still_runs_no_statement(self) -> None:
        # The tokenizer drops comments, and a note about why the failure is
        # discarded does not change that it is discarded.
        handlers = self._handlers("try { a(); } catch (e) { /* defaults are fine */ }")
        self.assertTrue(handlers[0].empty)

    def test_a_handler_that_reports_through_the_interface_is_not_called_empty(self) -> None:
        # This is the case the first version got wrong.
        handlers = self._handlers('try { a(); } catch { setError("could not save"); }')
        self.assertFalse(handlers[0].empty)
        self.assertFalse(handlers[0].rethrows)

    def test_nested_braces_do_not_end_the_body_early(self) -> None:
        handlers = self._handlers("try { a(); } catch (e) { if (x) { throw e; } }")
        self.assertTrue(handlers[0].rethrows)
        self.assertFalse(handlers[0].empty)

    def test_the_claim_counts_handlers_and_names_the_discarded_ones(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.ts").write_text(
                "export function go() {\n"
                "  try { a(); } catch { }\n"
                "  try { b(); } catch (e) { throw e; }\n"
                "}\n",
                encoding="utf-8",
            )
            result = analyze_snapshot(scan_repository(root))
            claim = next(
                item
                for item in result.claims
                if item.category == "caught_exception" and item.produced_by.startswith("typescript")
            )
            self.assertIn("2 place(s)", claim.claim)
            self.assertIn("1 of which rethrow", claim.claim)
            self.assertIn("run no statement at all", claim.claim)


class AnnotatedConstantTests(TestCase):
    """A typed codebase writes `const B: number = 10`, and none were read.

    The assignment had to sit two tokens after the name, so every annotated
    declaration was skipped -- which is the ordinary form in TypeScript. A
    validation library measured against its own source lost its patterns and
    its limits that way.
    """

    def test_an_annotated_number_is_recorded(self) -> None:
        found = _tunables(_tokens("export const LIMIT: number = 9;\n"))
        self.assertEqual(found["LIMIT"]["value"], "9")

    def test_an_annotated_string_is_recorded(self) -> None:
        found = _value_constants(_tokens('export const MODE: string = "surf";\n'))
        self.assertEqual(found["MODE"]["value"], "surf")

    def test_a_named_pattern_is_recorded_as_written(self) -> None:
        # A named pattern is a policy: it states which inputs the program
        # accepts. The tokenizer consumed regex literals without emitting
        # them, so a validation library declared two dozen of these and the
        # specification could not report the length of any of them.
        found = _value_constants(_tokens("export const nanoid: RegExp = /^[a-z0-9_-]{21}$/;\n"))
        self.assertEqual(found["nanoid"]["value"], "/^[a-z0-9_-]{21}$/")
        self.assertEqual(found["nanoid"]["literal"], "regex")

    def test_a_pattern_holding_a_quote_does_not_swallow_the_file(self) -> None:
        # The reason the body was discarded in the first place: a quote inside
        # a pattern, read as a string opener, once deleted every declaration
        # after it. Emitting the token must not reintroduce that.
        source = 'const q = /^"[a-z]+$/;\nexport const AFTER: number = 5;\n'
        self.assertEqual(_tunables(_tokens(source))["AFTER"]["value"], "5")

    def test_division_is_not_read_as_a_pattern(self) -> None:
        self.assertEqual(_value_constants(_tokens("const half = total / 2;\n")), {})

    def test_a_union_annotation_is_stepped_over(self) -> None:
        found = _tunables(_tokens("const RETRIES: number | null = 5;\n"))
        self.assertEqual(found["RETRIES"]["value"], "5")

    def test_an_annotated_negative_keeps_its_sign(self) -> None:
        found = _tunables(_tokens("const FLOOR: number = -9.8;\n"))
        self.assertEqual(found["FLOOR"]["value"], "-9.8")

    def test_a_generic_annotation_carries_no_literal(self) -> None:
        # `Map<string, number>` nests, and the value is a call rather than a
        # literal, so there is nothing to report -- but the brackets must not
        # send the scan past the end of the declaration either.
        self.assertEqual(_tunables(_tokens("const M: Map<string, number> = new Map();\n")), {})

    def test_a_function_type_annotation_is_not_mistaken_for_assignment(self) -> None:
        # `=>` tokenizes as `=` then `>`. Reading the arrow as the assignment
        # would take `void` as the value of the constant.
        self.assertEqual(_tunables(_tokens("const F: () => void = g;\n")), {})


class FunctionDeclarationScopeTests(TestCase):
    """A named function's body holds locals, not module constants.

    This asked only whether a function was assigned to something, and a
    declaration is not -- so `function f() { const scratch = 1; }` was treated
    like the IIFE wrapper, whose body genuinely does hold module constants.
    36 of zod's 54 reported constants were locals of that kind, most of them
    test data: `goodData`, `badData`, `errorMsg`, `tooLong`.
    """

    def test_a_local_in_a_declared_function_is_not_a_constant(self) -> None:
        source = "function timeSource(args) {\n  const hhmm = `x`;\n  const n: number = 2;\n}\n"
        self.assertEqual(_tunables(_tokens(source)), {})

    def test_the_iife_wrapper_still_holds_module_constants(self) -> None:
        source = "(function () {\n  const AIR_ACCEL = 12.0;\n})();\n"
        self.assertIn("AIR_ACCEL", _tunables(_tokens(source)))

    def test_a_local_in_an_assigned_function_expression_is_not_a_constant(self) -> None:
        source = "const f = function () {\n  const scratch = 1;\n};\n"
        self.assertEqual(_tunables(_tokens(source)), {})

    def test_a_local_in_a_method_is_not_a_constant(self) -> None:
        source = "class A {\n  run() {\n    const scratch = 2;\n  }\n}\n"
        self.assertEqual(_tunables(_tokens(source)), {})

    def test_a_module_constant_beside_a_declaration_survives(self) -> None:
        source = "export const LIMIT: number = 9;\nfunction f() {\n  const scratch = 1;\n}\n"
        found = _tunables(_tokens(source))
        self.assertEqual(sorted(found), ["LIMIT"])
