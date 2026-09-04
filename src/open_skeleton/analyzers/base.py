# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

from typing import Protocol

from open_skeleton.models import AnalysisResult, Snapshot

# What an analyzer counts as an eligible file, which decides what a low yield
# means about it. The two readings are not interchangeable and the document
# says different things about each:
#
#   "language"  Eligible is every file of the declared language. A file that
#               parsed and produced nothing is a real limit -- the grammar was
#               handled and the claim vocabulary had nothing to say -- so a low
#               yield belongs in "Where this analysis is thin".
#
#   "subject"   Eligible is every file carrying the thing this analyzer reads:
#               DDL, a published figure, a manifest. A repository where the
#               subject is simply absent must report zero eligible files, not
#               a hundred eligible files and no claims. Reporting the latter
#               put a measurement reader under the thin-coverage warning for
#               the crime of correctly finding no benchmarks in a tutorial.
#
# The distinction is declared rather than inferred because a wrong reading is
# invisible in the output: the claims are identical either way and only the
# denominator moves.
ELIGIBILITY_KINDS = frozenset({"language", "subject"})


class Analyzer(Protocol):
    name: str
    version: str
    eligibility: str

    def analyze(self, snapshot: Snapshot) -> AnalysisResult:
        """Analyze an immutable snapshot without executing target code."""


def render_declared_type(values: list[str], limit: int = 80) -> str:
    """Join type tokens the way the language wrote them.

    A space goes in exactly where leaving it out would merge two words:
    `dyn Error`, `keyof T`, `Augmentation extends ZodRawShape`. Joining
    everything with nothing between produced `Vec<Box<dynError>>` in Rust and
    `<AugmentationextendsZodRawShape>` in TypeScript, neither of which names a
    type a reader can search for.

    Punctuation gets no space of its own, so Java's wildcard reads
    `List<?extends Number>` rather than `List<? extends Number>`. That is one
    character off the idiom and still the right name to search for; spacing
    every operator the way each language prefers would be six rules instead
    of one, and this reads all six.

    Truncated at `limit`, because a fully instantiated generic signature can
    run to several hundred characters and a table has to stay readable.
    """

    rendered = ""
    for value in values:
        closes_a_word = bool(rendered) and (rendered[-1].isalnum() or rendered[-1] == "_")
        opens_a_word = value[:1].isalnum() or value[:1] == "_"
        if closes_a_word and opens_a_word:
            rendered += " "
        rendered += value
    return rendered.strip()[:limit]


def declares_a_number(value: object) -> bool:
    """Whether a recorded constant's value is a number rather than a string.

    The tunable index is titled for numbers and three readers were filing
    strings into it, so `SERVICE_NAME = "checkout"` appeared in a table of
    dials a maintainer would turn. TypeScript was corrected for this and Rust
    and Java were not, which is the shape of a rule that lives in one reader
    instead of in one place.

    Suffixes and separators are stripped because every language spells a
    literal number its own way: `10u32`, `1_000`, `1L`, `0x7fff`.
    """

    text = str(value).strip().replace("_", "")
    if not text:
        return False
    if text.lower().startswith(("0x", "0b", "0o")):
        return len(text) > 2
    stripped = text.rstrip("uUlLfFdD")
    for suffix in ("i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64", "f32", "f64", "usize"):
        stripped = stripped.removesuffix(suffix)
    try:
        float(stripped)
    except ValueError:
        return False
    return True
