# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

from typing import Any
from unittest import TestCase

from open_skeleton.audit import CENSUS_CATEGORIES, audit_claims


def _claim(claim_id: str, category: str, *evidence: str) -> dict[str, Any]:
    return {"claim_id": claim_id, "category": category, "supporting_evidence": list(evidence)}


class AuditTests(TestCase):
    """Every wrong answer so far was a true statement in the wrong frame."""

    def _checks(self, claims: Any, evidence: Any, files: Any) -> set[str]:
        return {item.check for item in audit_claims(tuple(claims), tuple(evidence), tuple(files))}

    def test_a_production_finding_evidenced_only_by_tests_is_flagged(self) -> None:
        # Flask registers sixteen routes inside its own suite. Each claim was
        # true; together they described a served surface that does not exist.
        checks = self._checks(
            [_claim("c1", "http_route", "e1")],
            [{"evidence_id": "e1", "path": "tests/test_app.py"}],
            [{"path": "tests/test_app.py", "role": "test"}],
        )
        self.assertIn("test-only-evidence", checks)

    def test_the_same_finding_from_application_code_is_not_flagged(self) -> None:
        checks = self._checks(
            [_claim("c1", "http_route", "e1")],
            [{"evidence_id": "e1", "path": "app/main.py"}],
            [{"path": "app/main.py", "role": "source"}],
        )
        self.assertNotIn("test-only-evidence", checks)

    def test_a_category_with_no_file_evidence_anywhere_is_flagged(self) -> None:
        # This is the shape in which an absence gets counted as presence. The
        # category has to be one that ought to name a file: `http_route` says
        # something about a handler, so a receipt naming no file is a gap.
        checks = self._checks(
            [_claim("c1", "http_route", "e1")],
            [{"evidence_id": "e1", "path": "."}],
            [],
        )
        self.assertIn("no-file-evidence", checks)

    def test_a_census_category_without_file_evidence_is_not_flagged(self) -> None:
        # "No CI workflow exists under .github/workflows" is a statement about
        # the repository and there is no file it could name without inventing
        # one. Flagging it fired on five of six repositories the first time
        # this ran across a set, always on the same categories, and a check
        # that reports the same thing everywhere teaches a reader to skip it.
        checks = self._checks(
            [_claim("c1", "delivery_automation", "e1")],
            [{"evidence_id": "e1", "path": "."}],
            [],
        )
        self.assertNotIn("no-file-evidence", checks)

    def test_a_monolith_is_not_flagged_for_concentration(self) -> None:
        # A service keeping every route in one module is normal. Flagging it
        # would fire on most repositories and teach a reader to skip this.
        claims = [_claim(f"r{i}", "http_route", f"e{i}") for i in range(8)]
        claims += [_claim(f"s{i}", "storage_schema", f"s{i}") for i in range(4)]
        evidence = [{"evidence_id": f"e{i}", "path": "app/main.py"} for i in range(8)]
        evidence += [{"evidence_id": f"s{i}", "path": "app/main.py"} for i in range(4)]
        files = [{"path": "app/main.py", "role": "source"}]
        self.assertNotIn("single-file-category", self._checks(claims, evidence, files))

    def test_a_category_concentrated_in_an_otherwise_quiet_file_is_flagged(self) -> None:
        claims = [_claim(f"o{i}", "odd_category", f"o{i}") for i in range(5)]
        claims += [_claim(f"m{i}", "other", f"m{i}") for i in range(40)]
        evidence = [{"evidence_id": f"o{i}", "path": "vendor/generated.py"} for i in range(5)]
        evidence += [{"evidence_id": f"m{i}", "path": "app/main.py"} for i in range(40)]
        files = [
            {"path": "vendor/generated.py", "role": "source"},
            {"path": "app/main.py", "role": "source"},
        ]
        self.assertIn("single-file-category", self._checks(claims, evidence, files))

    def test_one_claim_from_one_file_is_a_claim_not_a_pattern(self) -> None:
        checks = self._checks(
            [_claim("c1", "odd_category", "e1")],
            [{"evidence_id": "e1", "path": "a.py"}],
            [{"path": "a.py", "role": "source"}],
        )
        self.assertNotIn("single-file-category", checks)


class GateUsabilityTests(TestCase):
    """A gate that always fails is not a gate.

    `checked_out_revision` records the commit a snapshot was taken at. That is
    a property of the repository rather than of any file in it, so its receipt
    is a census receipt by construction and `no-file-evidence` fired on every
    git repository ever analyzed -- five of five here.

    This matters more than a stray finding. The intended use of
    `audit --strict` is a gate an agent consults before accepting work, and a
    gate that rejects every change for a reason no change can clear does not
    make review stricter. It teaches whoever wired it to turn the gate off,
    which is strictly worse than never having had one.
    """

    def _findings(self, category: str) -> set[str]:
        return {
            item.check
            for item in audit_claims(
                (_claim("c1", category, "e1"),),
                ({"evidence_id": "e1", "path": "."},),
                ({"path": "app/main.py", "role": "source"},),
            )
        }

    def test_the_checked_out_commit_is_not_reported_as_a_gap(self) -> None:
        self.assertEqual(self._findings("checked_out_revision"), set())

    def test_a_category_that_should_name_a_file_is_still_reported(self) -> None:
        # The exemption must stay narrow: a route claim naming no file is
        # exactly the shape this check exists to catch.
        self.assertIn("no-file-evidence", self._findings("http_route"))

    def test_every_exempt_category_is_actually_census_shaped(self) -> None:
        # A category earns its exemption by being unable to name a file, not
        # by being noisy. Each of these is a statement about the repository.
        for category in CENSUS_CATEGORIES:
            self.assertEqual(self._findings(category), set(), category)
