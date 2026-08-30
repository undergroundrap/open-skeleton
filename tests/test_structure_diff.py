# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""The structural benchmark must judge content, not labels.

Title matching scored a baseline section headed "Endpoint Catalog and Response
Conventions" as an untreated subject against a candidate that answered it
under the heading "HTTP Interface", across a thousand lines of endpoint
tables. These tests hold the second measure to separating cases that genuinely
differ, because a measure that says "covered" about everything is worth
exactly as little as one that says "missing".
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "comparison"))

from run_structure_diff import (
    _content_covered,
    _distinctive_terms,
    _section_bodies,
    _sections,
)

BASELINE = """\
## Endpoint Catalog and Response Conventions

Routes are `GET /health`, `POST /action`, `GET /player`, and each returns
`HTTPException` with `status_code` from `RESPONSE_CODES`.

## Alert Threshold Matrices

Page on-call when `p99_latency_ms` exceeds `LATENCY_BUDGET`, when
`error_rate` exceeds `ERROR_BUDGET`, or when `queue_depth` passes
`DEPTH_ALARM`.

## Shared Vocabulary

The system is robust and the design is considered.
"""

CANDIDATE = """\
## HTTP Interface

| Method | Path | Refuses with |
|---|---|---|
| `GET` | `/health` | — |
| `POST` | `/action` | `HTTPException` |
| `GET` | `/player` | `status_code` |

`RESPONSE_CODES` is declared once.
"""


class ContentMatchTests(TestCase):
    def setUp(self) -> None:
        self.sections = _sections(BASELINE)
        self.terms = _distinctive_terms(self.sections, _section_bodies(BASELINE))
        self.candidate = CANDIDATE.casefold()

    def _verdict(self, needle: str) -> bool | None:
        for section, terms in zip(self.sections, self.terms, strict=True):
            if needle in section.title:
                # The benchmark scripts sit outside the typed package, so the
                # call is untyped here; naming the type is what keeps the
                # three-way verdict from collapsing into a truthiness test.
                verdict: bool | None = _content_covered(terms, self.candidate, 0.5)
                return verdict
        raise AssertionError(f"no section titled {needle!r}")

    def test_a_subject_answered_under_another_heading_counts_as_covered(self) -> None:
        self.assertIs(self._verdict("Endpoint Catalog"), True)

    def test_a_subject_the_candidate_never_names_counts_as_absent(self) -> None:
        # Nothing static can evidence an alert threshold that exists in no
        # code, and the measure must keep saying so.
        self.assertIs(self._verdict("Alert Threshold"), False)

    def test_a_prose_only_subject_is_not_scored_either_way(self) -> None:
        self.assertIsNone(self._verdict("Shared Vocabulary"))

    def test_a_term_every_section_uses_cannot_identify_a_subject(self) -> None:
        document = "## A\n\n`common`\n\n## B\n\n`common`\n\n## C\n\n`common`\n"
        sections = _sections(document)
        terms = _distinctive_terms(sections, _section_bodies(document))
        self.assertFalse(any("common" in item for item in terms))

    def test_section_bodies_align_with_the_sections_they_describe(self) -> None:
        bodies = _section_bodies(BASELINE)
        self.assertEqual(len(bodies), len(self.sections))
        self.assertIn("RESPONSE_CODES", bodies[0])
        self.assertIn("LATENCY_BUDGET", bodies[1])

    def test_code_fence_comments_are_not_counted_as_document_headings(self) -> None:
        document = (
            "# Real heading\n\n"
            "```python\n"
            "# Example comment\n"
            "## Example pseudo-heading\n"
            "```\n\n"
            "## Second real heading\n"
        )

        sections = _sections(document)
        bodies = _section_bodies(document)

        self.assertEqual(
            [section.title for section in sections], ["Real heading", "Second real heading"]
        )
        self.assertIn("Example pseudo-heading", bodies[0])
