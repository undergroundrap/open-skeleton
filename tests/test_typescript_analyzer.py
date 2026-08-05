# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analyzers.typescript_lexical import TypeScriptLexicalAnalyzer, _tokens
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
