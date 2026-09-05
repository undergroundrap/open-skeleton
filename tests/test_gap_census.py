# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""The census's two checks for an absence that is this engine's fault.

Everything else the census reports is ranked by breadth and judged by a
reader. These two are not: a probe that names a file the snapshot holds, or a
dependency the manifest declares, and matched nothing, is a defect with no
judgement required. So they are the part worth pinning.

They exist in this shape because the obvious version does not work. A section
absent "despite its own probes matching" cannot happen -- `evaluate_section`
defines absent as every probe matching nothing -- and a check for it read zero
on every corpus while looking like a clean bill of health. A false absence is
only visible to evidence the probe did not use.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "generalization"))

from run_gap_census import _missed_dependencies, _missed_globs, _reference_shape


class MissedGlobTests(TestCase):
    def test_a_glob_naming_a_file_in_the_snapshot_is_a_defect(self) -> None:
        missed = _missed_globs({("path_glob", "Dockerfile")}, ["deploy/Dockerfile"])
        self.assertEqual(missed, {("path_glob", "Dockerfile")})

    def test_a_glob_naming_nothing_is_the_repository_being_honest(self) -> None:
        self.assertEqual(_missed_globs({("path_glob", "Dockerfile")}, ["src/app.py"]), set())

    def test_only_path_globs_are_checked_this_way(self) -> None:
        self.assertEqual(_missed_globs({("dependency_name", "stripe")}, ["stripe"]), set())


class MissedDependencyTests(TestCase):
    MANIFEST = '{"dependencies": {"stripe": "^14.0.0", "@aws-sdk/client-s3": "^3.400.0"}}'

    def test_a_declared_dependency_the_probe_missed_is_a_defect(self) -> None:
        missed = _missed_dependencies({("dependency_name", "stripe")}, self.MANIFEST)
        self.assertEqual(missed, {("dependency_name", "stripe")})

    def test_a_scoped_glob_finds_its_scope(self) -> None:
        # The separator goes with the star. Keeping the slash in the stem made
        # every scoped package silently unmatchable, which is the kind of
        # quiet nothing this whole check exists to avoid.
        missed = _missed_dependencies({("dependency_name", "@aws-sdk/*")}, self.MANIFEST)
        self.assertEqual(missed, {("dependency_name", "@aws-sdk/*")})

    def test_a_dependency_the_manifest_does_not_declare_is_not_a_defect(self) -> None:
        self.assertEqual(
            _missed_dependencies({("dependency_name", "braintree")}, self.MANIFEST), set()
        )

    def test_a_name_inside_another_name_is_not_a_dependency(self) -> None:
        # `ava` inside `java` is not a dependency on `ava`, and a census that
        # reported it would be ranking coincidence.
        self.assertEqual(
            _missed_dependencies({("dependency_name", "ava")}, '{"deps": {"java": "1"}}'), set()
        )

    def test_a_repository_with_no_manifest_reports_nothing(self) -> None:
        self.assertEqual(_missed_dependencies({("dependency_name", "stripe")}, ""), set())

    def test_a_very_short_term_is_not_matched(self) -> None:
        # Two characters match something in almost any manifest.
        self.assertEqual(
            _missed_dependencies({("dependency_name", "ai*")}, '{"deps": {"ai-sdk": "1"}}'), set()
        )


class ReferenceShapeTests(TestCase):
    """A reference is bucketed by shape because the text is one repository."""

    def test_shapes(self) -> None:
        self.assertEqual(_reference_shape("./model"), "relative path (./x)")
        self.assertEqual(_reference_shape("super::thing"), "module-relative (super::x)")
        self.assertEqual(_reference_shape("crate::thing"), "crate-root (crate::x)")
        self.assertEqual(_reference_shape("@scope/pkg"), "scoped package (@scope/x)")
        self.assertEqual(_reference_shape("std::io"), "namespaced (a::b)")
        self.assertEqual(_reference_shape("a.b"), "dotted (a.b)")
        self.assertEqual(_reference_shape("requests"), "bare name")
