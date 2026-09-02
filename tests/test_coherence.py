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
from open_skeleton.spec.render import (
    ABSENCE_HEADING,
    CLAIM_PAGE,
    _languages_no_analyzer_read,
)
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

    def test_a_populated_projection_prevents_an_absent_verdict(self) -> None:
        # The concentration graph is computed from the file census. Its probe
        # can miss when a small repository does not cross the concentration
        # claim threshold, but the graph still establishes the concern.
        section = next(
            item
            for item in self._sample().sections
            if item.section_id == "introduction.concentration"
        )

        self.assertTrue(any(diagram.mermaid for diagram in section.diagrams))
        self.assertEqual(section.verdict, "evidenced")

    def test_absence_announced_above_a_populated_projection_is_caught(self) -> None:
        document = self._sample()
        target = next(item for item in document.sections if any(x.mermaid for x in item.diagrams))
        broken = replace(
            document,
            sections=tuple(
                replace(item, verdict="absent", findings=()) if item is target else item
                for item in document.sections
            ),
        )

        self.assertIn("verdict-contradicts-projection", self._checks(broken))

    def test_a_wrong_absence_tally_is_still_caught(self) -> None:
        # The tally check finds its paragraph by heading and reports "coherent"
        # when it finds none, so renaming the section in the renderer alone
        # would disable it silently. Both sides now import one constant; this
        # proves the checker still reaches the paragraph by corrupting the
        # count and requiring the complaint.
        document = self._sample()
        markdown = render_spec_markdown(document)
        self.assertIn(ABSENCE_HEADING, markdown)
        target = next((item for item in document.sections if item.verdict == "absent"), None)
        self.assertIsNotNone(target, "the fixture should leave some concern absent")
        assert target is not None
        fewer = replace(
            document,
            sections=tuple(
                replace(item, verdict="structural") if item is target else item
                for item in document.sections
            ),
        )
        self.assertIn("absence-tally-disagrees", self._checks(fewer, markdown))

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


class EnumerationRobustnessTests(TestCase):
    """The checker that keeps a document honest must not prevent one existing.

    `[\\d,]+` also matches a bare comma, so an ordinary claim sentence --
    "throws in 7 place(s), of 2 distinct type(s)" -- captured "," as a count
    and crashed on `int("")`. A whole repository produced no specification at
    all until this was fixed, and nothing in the suite noticed because no
    analyzer had written that phrasing before.
    """

    def _sample(self) -> Any:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            return _document(root, workspace / "state")

    def _checks(self, markdown: str) -> set[str]:
        return {item.check for item in check_coherence(self._sample(), markdown)}

    def test_a_comma_before_of_is_not_a_count(self) -> None:
        markdown = (
            "Assets/Creature.cs throws in 7 place(s), of 2 distinct type(s): "
            "`ArgumentNullException`, `InvalidOperationException`, `IOException`."
        )
        self.assertNotIn("enumeration-truncated-silently", self._checks(markdown))

    def test_a_stated_count_larger_than_its_list_is_still_reported(self) -> None:
        # The fix must not buy robustness by disabling the check.
        markdown = "Covers 40 of 60 concerns: `alpha`, `beta`, `gamma`."
        self.assertIn("enumeration-truncated-silently", self._checks(markdown))

    def test_a_declared_remainder_clears_the_finding(self) -> None:
        markdown = "Covers 40 of 60 concerns: `alpha`, `beta`, `gamma` and 37 more."
        self.assertNotIn("enumeration-truncated-silently", self._checks(markdown))


class UnreadLanguageTests(TestCase):
    """An absence resting on an unread file is not an absence.

    `_unread_files` counts files an analyzer declared eligible and failed to
    parse. A language with no analyzer at all never appears in a coverage
    record, so the document said "every eligible file parsed ... not by
    anything left unread" about a repository holding a shell script nothing
    had opened.
    """

    FILES = (
        {"path": "app.py", "language": "Python"},
        {"path": "install.sh", "language": "Shell"},
        {"path": "run.bat", "language": "Batch"},
    )

    def test_a_language_no_analyzer_touched_is_named(self) -> None:
        found = dict(_languages_no_analyzer_read(self.FILES, [{"path": "app.py"}], []))
        self.assertEqual(found, {"Shell": 1, "Batch": 1})

    def test_a_language_reached_only_through_evidence_counts_as_read(self) -> None:
        # An analyzer may cite a file without emitting a symbol for it.
        found = dict(
            _languages_no_analyzer_read(self.FILES, [{"path": "app.py"}], [{"path": "install.sh"}])
        )
        self.assertEqual(found, {"Batch": 1})

    def test_a_language_partly_read_is_not_reported_as_unread(self) -> None:
        # The claim is that *nothing* read the language. One unparsed file of a
        # covered language is a parse failure, which `_unread_files` reports.
        files = (
            {"path": "a.py", "language": "Python"},
            {"path": "b.py", "language": "Python"},
        )
        self.assertEqual(_languages_no_analyzer_read(files, [{"path": "a.py"}], []), ())

    def test_nothing_is_reported_when_everything_was_read(self) -> None:
        self.assertEqual(
            _languages_no_analyzer_read(
                self.FILES,
                [{"path": item["path"]} for item in self.FILES],
                [],
            ),
            (),
        )

    def test_an_eligible_but_unparsed_language_is_not_reported_twice(self) -> None:
        """Two shortfalls with one cause read as two causes.

        A language an analyzer declares eligible and then does not parse is
        already named by the existing check. Naming it again as unread said
        the same thing twice in different words.
        """

        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            (root / "example.hum").write_text("let x = 1\n", encoding="utf-8")
            markdown = render_spec_markdown(_document(root, workspace / "state"))
            self.assertIn("eligible file(s)", markdown)
            equipped = markdown.split("no analyzer is equipped to read", 1)
            if len(equipped) > 1:
                self.assertNotIn("Hum", equipped[1].split(".", 1)[0])

    def test_the_document_says_so_where_it_discusses_absence(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            (root / "install.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
            document = _document(root, workspace / "state")
            self.assertIn("Shell", dict(document.unread_languages))
            markdown = render_spec_markdown(document)
            self.assertIn("no analyzer is equipped to read", markdown)


class StructuralSentenceTests(TestCase):
    """A structural section with no subsections organizes nothing.

    Six sections carried tables and told the reader they organized the
    subsections below them. There were none. A document that misdescribes its
    own shape is the failure this module exists to catch, and it does not stop
    being one because the sentence is boilerplate.
    """

    def _sentences(self) -> dict[str, str]:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            markdown = render_spec_markdown(_document(root, workspace / "state"))
        found: dict[str, str] = {}
        heading: str | None = None
        for line in markdown.splitlines():
            if line.startswith("#"):
                heading = line.lstrip("# ").strip()
            elif heading and "makes no presence claim" in line:
                found.setdefault(heading, line.strip())
        return found

    def test_a_parent_section_says_it_organizes_its_children(self) -> None:
        sentences = self._sentences()
        parents = [text for text in sentences.values() if "organizes the subsections" in text]
        self.assertTrue(parents, "expected at least one parent section")

    def test_a_leaf_section_does_not_claim_subsections(self) -> None:
        sentences = self._sentences()
        leaves = [
            heading
            for heading, text in sentences.items()
            if "drawn from evidence established elsewhere" in text
        ]
        self.assertTrue(leaves, "expected at least one panel-only section")
        for heading in leaves:
            self.assertNotIn("organizes the subsections", sentences[heading])
