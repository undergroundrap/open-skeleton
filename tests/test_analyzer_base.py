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

from unittest import TestCase

from open_skeleton.analyzers.base import render_declared_type


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
