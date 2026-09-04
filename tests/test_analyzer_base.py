# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""The predicates three readers share.

`render_declared_type` exists because each reader that reports a field's type
joined its tokens with nothing between them. That is right for punctuation and
wrong for two words in a row, and the effect had been shipping: Rust rendered
`Vec<Box<dyn Error>>` as `Vec<Box<dynError>>`, and the TypeScript reader
produced `<AugmentationextendsZodRawShape>` against zod.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton import analyzers
from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.base import render_declared_type
from open_skeleton.scanner import scan_repository


class RenderDeclaredTypeTests(TestCase):
    def test_punctuation_needs_no_space(self) -> None:
        self.assertEqual(
            render_declared_type(["Map", "<", "String", ",", "Integer", ">"]),
            "Map<String,Integer>",
        )

    def test_two_words_are_separated(self) -> None:
        self.assertEqual(
            render_declared_type(["Vec", "<", "Box", "<", "dyn", "Error", ">", ">"]),
            "Vec<Box<dyn Error>>",
        )

    def test_a_word_after_punctuation_stays_closed_up(self) -> None:
        # One rule reads all six languages. Spacing each operator the way its
        # own language prefers would be six rules, and `List<?extends Number>`
        # is one character off the idiom and still searchable.
        self.assertEqual(
            render_declared_type(["List", "<", "?", "extends", "Number", ">"]),
            "List<?extends Number>",
        )

    def test_an_underscore_counts_as_part_of_a_word(self) -> None:
        self.assertEqual(render_declared_type(["my_type", "other"]), "my_type other")

    def test_a_long_signature_is_truncated(self) -> None:
        self.assertEqual(len(render_declared_type(["x" * 200])), 80)

    def test_nothing_renders_as_nothing(self) -> None:
        self.assertEqual(render_declared_type([]), "")


class LineEndingTests(TestCase):
    """A reader has to read the file that is on disk.

    A `\r\n` file is not an edge case on Windows; it is the ordinary case.
    The pipeline decodes the bytes it hashed, so a pattern anchored at `$`
    meets a carriage return in that position and matches nothing. One did, in
    the PowerShell shape reader, and it read every CRLF file as empty while
    every check agreed with it.
    """

    def test_no_reader_anchors_at_a_carriage_return(self) -> None:
        risky: list[str] = []
        for info in pkgutil.iter_modules(analyzers.__path__):
            module = importlib.import_module(f"open_skeleton.analyzers.{info.name}")
            for name, value in vars(module).items():
                if not isinstance(value, re.Pattern):
                    continue
                pattern = value.pattern.rstrip()
                multiline = bool(value.flags & re.MULTILINE) or "(?m" in pattern
                if multiline and pattern.endswith("$") and not pattern.endswith(r"\r?$"):
                    risky.append(f"{info.name}.{name}")
        self.assertEqual(risky, [], "multiline pattern(s) that a CRLF file will not match")

    def test_every_reader_finds_the_same_thing_in_a_crlf_file(self) -> None:
        # Written with `newline=""` so the carriage returns survive: the
        # default translation is exactly what hid the defect the first time.
        sources = {
            "policy.py": 'MAX_RETRIES = 10\r\nSERVICE_NAME = "checkout"\r\n',
            "policy.ts": "export const MAX_RETRIES: number = 10;\r\n",
            "policy.rs": "pub const MAX_RETRIES: u32 = 10;\r\n",
            "Policy.java": "class P { static final int MAX_RETRIES = 10; }\r\n",
            "Policy.cs": "class P { public const int MaxRetries = 10; }\r\n",
            "policy.ps1": "$script:MaxRetries = 10\r\n",
        }
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, body in sources.items():
                with (root / name).open("w", encoding="utf-8", newline="") as handle:
                    handle.write(body)
            result = analyze_snapshot(scan_repository(root))

        languages: set[str] = set()
        for symbol in result.symbols:
            metadata = symbol.metadata or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            if metadata.get("tunables"):
                languages.add(symbol.language or "")
        self.assertEqual(
            len(languages),
            len(sources),
            f"only {sorted(languages)} reported a tunable from a CRLF file",
        )
