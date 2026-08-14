# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""What every analyzer owes the document, checked across all of them at once.

This project's most repeated defect is not a wrong answer. It is a correct fix
landing in one analyzer and never crossing to the other three, which has now
happened often enough to be the thing worth testing directly. Two analyzers
shipped on the same afternoon disagreed about what an eligible file is, and
the disagreement was invisible: the claims were identical either way and only
the denominator moved, so a reader was told an analysis was thin when the
subject was merely absent.

These tests iterate the registered analyzers rather than naming any, because a
list of names is the failure this project already knows -- a category missing
from a frozenset passed every check while being wrong.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot, build_analyzers
from open_skeleton.analyzers.base import ELIGIBILITY_KINDS
from open_skeleton.scanner import scan_repository

# Ordinary code and prose, carrying no schema, no published figure, and no
# manifest. Every subject-scoped analyzer must find nothing to be eligible for
# here. A new analyzer whose subject genuinely appears in this fixture should
# extend the fixture rather than exempt itself.
UNREMARKABLE = {
    "app.py": "def add(a, b):\n    return a + b\n",
    "README.md": "# Demo\n\nA module that adds two numbers together.\n",
}


class AnalyzerContractTests(TestCase):
    def _analyzers(self) -> tuple[Any, ...]:
        return build_analyzers(())

    def test_every_analyzer_declares_what_makes_a_file_eligible(self) -> None:
        # Declared rather than inferred, because getting it wrong changes no
        # claim and no test output -- only the yield denominator, and through
        # it what the document says about its own reliability.
        for analyzer in self._analyzers():
            with self.subTest(analyzer=analyzer.name):
                self.assertIn(
                    getattr(analyzer, "eligibility", None),
                    ELIGIBILITY_KINDS,
                    f"{analyzer.name} must declare eligibility as one of "
                    f"{sorted(ELIGIBILITY_KINDS)}",
                )

    def test_a_subject_analyzer_claims_nothing_where_its_subject_is_absent(self) -> None:
        # The invariant the declaration buys. A subject-scoped analyzer facing
        # a repository with none of its subject must report zero eligible
        # files, not "I read twenty and understood none" -- that reading is
        # reserved for an analyzer that met its language and failed on it.
        subject = {
            item.name for item in self._analyzers() if getattr(item, "eligibility", "") == "subject"
        }
        self.assertTrue(subject, "the fixture assumes at least one subject-scoped analyzer")
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            for name, body in UNREMARKABLE.items():
                (root / name).write_text(body, encoding="utf-8")
            coverage = analyze_snapshot(scan_repository(root)).coverage
        for record in coverage:
            if record.analyzer.split("/")[0] not in subject:
                continue
            with self.subTest(analyzer=record.analyzer):
                self.assertEqual(
                    record.eligible_files,
                    0,
                    f"{record.analyzer} claims {record.eligible_files} eligible file(s) in a "
                    "repository holding none of its subject, so a zero yield there will be "
                    "reported as thin analysis rather than as nothing to say",
                )

    def test_every_analyzer_reports_coverage_under_the_version_it_declares(self) -> None:
        # `version` is read by nothing, which is how six analyzers came to hold
        # the qualified identity that appears in records ("python-ast/v2") and
        # two came to hold a bare "v1". Nothing broke, and nothing would have:
        # an attribute no code consumes drifts silently until someone reads it
        # and believes it. Tying it to the identity the coverage rows actually
        # carry makes the drift a failure instead of a discrepancy.
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            for name, body in UNREMARKABLE.items():
                (root / name).write_text(body, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
        reported = {record.analyzer for record in result.coverage}
        for analyzer in self._analyzers():
            with self.subTest(analyzer=analyzer.name):
                self.assertIn(
                    analyzer.version,
                    reported,
                    f"{analyzer.name} declares version {analyzer.version!r}, which is not the "
                    "identity any of its coverage rows carry",
                )
