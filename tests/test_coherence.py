# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Checks that the document agrees with itself.

Each case here reproduces a defect that shipped and was found by reading. The
point of the module under test is that reading is no longer the only thing
that finds them.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository
from open_skeleton.spec import build_spec, every_claim, load_profile, render_spec_markdown
from open_skeleton.spec.capabilities import Capability
from open_skeleton.spec.coherence import check_coherence, check_conservation
from open_skeleton.spec.render import CLAIM_PAGE
from tests.helpers import create_sample_repository


def _document(root: Path, state: Path) -> Any:
    snapshot = scan_repository(root)
    ledger = EvidenceLedger(state / "evidence.sqlite3")
    ledger.save_snapshot(snapshot)
    ledger.save_analysis(analyze_snapshot(snapshot))
    return build_spec(ledger, load_profile())


class CoherenceTests(TestCase):
    def _sample(self) -> Any:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            return _document(root, workspace / "state")

    def _checks(self, document: Any, markdown: str | None = None) -> set[str]:
        rendered = render_spec_markdown(document) if markdown is None else markdown
        return {item.check for item in check_coherence(document, rendered)}

    def test_a_sound_document_reports_nothing(self) -> None:
        self.assertEqual(self._checks(self._sample()), set())

    def test_absence_announced_above_findings_is_caught(self) -> None:
        # Runtime Topology read "Determination: absent. Every probe declared
        # for this concern returned zero matches" directly above seven
        # verified findings, and the summary counted it among the concerns
        # the repository does not implement.
        document = self._sample()
        target = next((item for item in document.sections if item.findings), None)
        self.assertIsNotNone(target, "the fixture should render findings somewhere")
        assert target is not None
        broken = replace(
            document,
            sections=tuple(
                replace(item, verdict="absent") if item is target else item
                for item in document.sections
            ),
        )
        self.assertIn("verdict-contradicts-findings", self._checks(broken))

    def test_a_silently_truncated_list_is_caught(self) -> None:
        # "21 of 48" followed by ten names, with no ellipsis and no
        # remainder, so a reader stops at the tenth believing it is all.
        document = replace(
            self._sample(),
            capabilities=tuple(
                Capability(
                    capability_id=f"C-{index:03d}",
                    label=f"feature{index}",
                    kind="module",
                    routes=(),
                    symbols=(f"pkg.feature{index}.run",),
                    paths=(f"pkg/feature{index}.py",),
                    claim_ids=(),
                    evidence_ids=(),
                    exercised_by=(),
                )
                for index in range(1, 26)
            ),
        )
        honest = render_spec_markdown(document)
        self.assertEqual(self._checks(document, honest), set())
        truncated = re.sub(r", and [\d,]+ more", "", honest)
        self.assertIn("enumeration-truncated-silently", self._checks(document, truncated))

    def test_a_wrong_remainder_is_caught(self) -> None:
        document = self._sample()
        markdown = render_spec_markdown(document).replace(
            "## Executive summary",
            "## Executive summary\n\n30 of 60: `a`, `b`, `c`, and 2 more.",
            1,
        )
        self.assertIn("enumeration-remainder-wrong", self._checks(document, markdown))

    def test_a_determination_table_that_drops_a_verdict_is_caught(self) -> None:
        # The table listed four of the profile's verdicts by name, so a
        # section holding any other vanished from it.
        document = self._sample()
        markdown = render_spec_markdown(document)
        row = re.search(r"^\| [a-z_]+ \| [\d,]+ \|$", markdown, re.MULTILINE)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn(
            "determination-summary-incomplete",
            self._checks(document, markdown.replace(row.group(0) + "\n", "", 1)),
        )

    def test_a_claim_rendered_into_two_sections_is_caught(self) -> None:
        document = self._sample()
        source = next(item for item in document.sections if item.findings)
        other = next(item for item in document.sections if item is not source)
        broken = replace(
            document,
            sections=tuple(
                replace(item, findings=source.findings) if item is other else item
                for item in document.sections
            ),
        )
        self.assertIn("claim-rendered-twice", self._checks(broken))

    def test_a_capability_tally_that_disagrees_is_caught(self) -> None:
        document = self._sample()
        markdown = render_spec_markdown(document).replace(
            "## Executive summary",
            "### Capabilities with no verifying reference\n\n"
            "99 of 3: `a`, `b`, `c`. Static resolution cannot see dynamic dispatch.",
            1,
        )
        found = self._checks(document, markdown)
        self.assertIn("capability-tally-disagrees", found)
        self.assertIn("capability-total-disagrees", found)

    def test_a_ratio_in_prose_is_not_mistaken_for_a_list(self) -> None:
        # "1 of 2 files" in a sentence with no enumeration under it is not a
        # truncated list, and reporting it would make the check useless.
        document = self._sample()
        markdown = render_spec_markdown(document).replace(
            "## Executive summary",
            "## Executive summary\n\n1 of 2 analyzers produced a finding.",
            1,
        )
        self.assertEqual(self._checks(document, markdown), set())


class ConservationTests(TestCase):
    """The document has to account for everything the ledger holds.

    Every other check in this module compares the document against itself,
    and that is a real limit rather than a quibble. The specification builder
    asked the ledger for one page of claims and reported the page size as the
    total: the document announced 5,000 claims for a snapshot holding 8,707,
    and every internal check passed, because the document was perfectly
    consistent about a number that was already wrong when it arrived.

    Reconciling against counts measured at the source is the only way to see
    that class of defect at all.
    """

    def _document(self) -> Any:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            return _document(root, workspace / "state")

    def test_a_document_holding_everything_reports_nothing(self) -> None:
        document = self._document()
        self.assertEqual(check_conservation(document, dict(document.source_counts)), ())

    def test_a_truncated_claim_total_is_caught(self) -> None:
        document = self._document()
        counts = {"claims": document.total_claims + 3_707}
        found = {item.check for item in check_conservation(document, counts)}
        self.assertIn("claims-not-conserved", found)

    def test_a_truncated_symbol_index_is_caught(self) -> None:
        document = self._document()
        self.assertTrue(document.symbols, "the fixture should carry symbols")
        counts = {"symbols": len(document.symbols) + 638}
        found = {item.check for item in check_conservation(document, counts)}
        self.assertIn("symbols-not-conserved", found)

    def test_a_truncated_edge_graph_is_caught(self) -> None:
        # Edges never reach the rendered document, so nothing could be
        # counted from it. Capability traceability is computed from them: a
        # call edge lost to a page boundary reports a capability as reached
        # by no test when a test reaches it.
        document = self._document()
        counts = {"edges": document.source_counts["edges"] + 1_865}
        found = {item.check for item in check_conservation(document, counts)}
        self.assertIn("edges-not-conserved", found)

    def test_a_headline_total_that_drifts_from_its_rows_is_caught(self) -> None:
        document = replace(self._document(), total_claims=5_000)
        found = {item.check for item in check_conservation(document, {})}
        self.assertIn("claim-total-misreported", found)

    def test_counts_the_ledger_cannot_supply_are_not_invented(self) -> None:
        # A caller that measured nothing gets no verdict, rather than a
        # comparison against zero that would fail every document.
        self.assertEqual(check_conservation(self._document(), {}), ())


class PagingTests(TestCase):
    """Reading past the first page, and reading each row exactly once.

    Three projections asked the ledger for a bounded page and treated the
    result as everything it held: claims capped at 5,000 against a snapshot
    with 8,707, symbols at 5,000 against 5,638, and edges at 20,000 against
    21,865. The edge case was the worst of the three because capability
    traceability is computed from edges, so a call edge falling off the end
    of a page reports a capability as reached by no test.
    """

    class _Ledger:
        """A ledger stub that honours limit and offset, and counts calls."""

        def __init__(self, total: int) -> None:
            self.rows = [{"claim_id": f"c{index:05d}"} for index in range(total)]
            self.calls = 0

        def list_claims(self, snapshot_id: str, *, limit: int, offset: int = 0) -> list[Any]:
            self.calls += 1
            return self.rows[offset : offset + limit]

    def _read(self, total: int) -> tuple[list[Any], int]:
        ledger = self._Ledger(total)
        found = every_claim(ledger, "snapshot")  # type: ignore[arg-type]
        return found, ledger.calls

    def test_a_snapshot_larger_than_one_page_is_read_whole(self) -> None:
        found, _ = self._read(CLAIM_PAGE * 2 + 137)
        self.assertEqual(len(found), CLAIM_PAGE * 2 + 137)

    def test_no_row_is_read_twice_or_skipped(self) -> None:
        found, _ = self._read(CLAIM_PAGE + 1)
        identifiers = [item["claim_id"] for item in found]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual(identifiers, sorted(identifiers))

    def test_an_exact_page_multiple_still_terminates(self) -> None:
        # A full final page is indistinguishable from a truncated one without
        # asking once more, so the loop must not stop on a full page.
        found, calls = self._read(CLAIM_PAGE)
        self.assertEqual(len(found), CLAIM_PAGE)
        self.assertEqual(calls, 2)

    def test_an_empty_snapshot_costs_one_query(self) -> None:
        found, calls = self._read(0)
        self.assertEqual(found, [])
        self.assertEqual(calls, 1)
