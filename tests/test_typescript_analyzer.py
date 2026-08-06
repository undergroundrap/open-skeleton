# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analyzers.typescript_lexical import (
    TypeScriptLexicalAnalyzer,
    _declarations,
    _environment_reads,
    _external_origins,
    _imported_names,
    _module_state,
    _object_keys,
    _parameter_names,
    _references,
    _throw_sites,
    _tokens,
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
