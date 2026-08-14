# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Telling a figure that was observed from a figure that was wanted.

Every case here was found by running the classifier over the documentation of
the repositories on this machine and reading what it decided. The failure mode
is one-directional and quiet: a budget filed as a measurement becomes evidence
the system is fast, which is a claim nobody in the repository made. A
measurement filed as a budget only loses a fact.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.documented_measurements import ANALYZER_VERSION, classify, scan
from open_skeleton.scanner import scan_repository


class ClassifyTests(TestCase):
    def test_a_bound_before_a_number_is_a_budget(self) -> None:
        self.assertEqual(classify("p95 lookup: under 200 ns"), "budget")
        self.assertEqual(classify("RAM after 10 min | < 1.5 GB"), "budget")

    def test_a_past_tense_report_is_a_measurement(self) -> None:
        self.assertEqual(classify("The probe completed in 2,893 ms"), "measured")
        self.assertEqual(classify("the run took 16 seconds"), "measured")

    def test_a_bound_word_used_as_a_preposition_is_not_a_budget(self) -> None:
        # "meshed from a distance-ordered queue under a 1.2 ms budget" is a
        # budget; "baked under load in 3 ms" is not, and the difference is
        # whether a number follows the word. Reading every "under" as a bound
        # relabelled an entire measured timing table as a table of targets.
        self.assertIsNone(classify("work proceeds under the render thread at 5 ms"))

    def test_a_percentile_label_decides_nothing_by_itself(self) -> None:
        # p99 names which figure is reported, not whether it was observed. It
        # appears in "p95 lookup: under 200 ns" and in a measured results row
        # alike, so on its own it must classify neither way.
        self.assertIsNone(classify("| CPU per frame, p99 | 0.40 ms |"))

    def test_a_summary_statistic_decides_nothing_by_itself(self) -> None:
        # Same reasoning as the percentile: "mean" is symmetric with "p99",
        # and treating it as proof of measurement read a benchmark *plan* as a
        # record of results.
        self.assertIsNone(classify("| Frame time, mean | 0.13 ms |"))

    def test_a_line_saying_both_things_is_dropped(self) -> None:
        self.assertIsNone(classify("measured at 5 ms against a target of under 10 ms"))

    def test_a_line_with_no_quantity_is_not_a_finding(self) -> None:
        self.assertIsNone(classify("the benchmark completed successfully"))

    def test_a_bare_number_is_not_a_quantity(self) -> None:
        # Version numbers, years and counts are numbers in documentation, and
        # a reader keyed on digits alone reports all of them.
        self.assertIsNone(classify("Requires Python 3.12 and version 2 of the profile"))


class ScanTests(TestCase):
    def test_a_table_inherits_the_framing_of_the_line_introducing_it(self) -> None:
        # "| `check` | 0 | 308 ms |" states a measurement to a person and
        # nothing at all to a reader that looks only at the row.
        text = "The clean-baseline probe completed in 2,893 ms:\n\n| step | n | time |\n|---|---|---|\n| `check` | 0 | 308 ms |\n"
        found = scan(text)
        self.assertIn((5, "measured", "| `check` | 0 | 308 ms |"), found)

    def test_a_budget_is_never_inherited(self) -> None:
        # A sentence that states a target and then introduces measured rows
        # relabelled every one of them. Targets are written per line in
        # practice, so inheriting one buys nothing and risks exactly that.
        text = "Everything runs under a 1.2 ms budget. Individual costs:\n\n| op | cost |\n|---|---|\n| bake | 1.01 ms |\n"
        self.assertEqual([item for item in scan(text) if item[0] == 5], [])

    def test_a_framing_line_must_introduce_something(self) -> None:
        # Without the colon, any paragraph mentioning timing anywhere above an
        # unrelated table would confer its reading on every row.
        text = "The probe completed in 2,893 ms once.\n\nUnrelated notes.\n\n| op | cost |\n|---|---|\n| bake | 1.01 ms |\n"
        self.assertEqual([item for item in scan(text) if item[0] == 7], [])

    def test_fenced_code_is_never_read(self) -> None:
        # A timeout constant in an example measures nothing.
        text = "```python\nTIMEOUT_MS = 500  # took 20 ms\n```\n"
        self.assertEqual(scan(text), [])

    def test_a_table_separator_row_is_not_a_finding(self) -> None:
        text = "Measured:\n\n| a | b |\n|---|---:|\n| x | 5 ms |\n"
        self.assertEqual([item for item in scan(text) if "---" in item[2]], [])

    def test_framing_does_not_survive_past_its_table(self) -> None:
        text = (
            "Measured:\n\n| a | b |\n|---|---|\n| x | 5 ms |\n\n"
            "Some later prose.\n\n| c | d |\n|---|---|\n| y | 9 ms |\n"
        )
        later = [item for item in scan(text) if item[2].strip().startswith("| y")]
        self.assertEqual(later, [])


class AnalyzerTests(TestCase):
    def _claims(self, sources: dict[str, str]) -> list[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            for name, body in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            return [claim.claim for claim in result.claims if claim.produced_by == ANALYZER_VERSION]

    def test_a_measurement_and_a_budget_never_share_wording(self) -> None:
        found = self._claims(
            {"PERF.md": ("The suite completed in 16 seconds.\n\nThe target is under 10 seconds.\n")}
        )
        self.assertTrue(any("records having measured" in item for item in found))
        self.assertTrue(any("states as a target" in item for item in found))
        self.assertFalse(
            any("records having measured" in item and "target is under" in item for item in found)
        )

    def test_a_repository_that_publishes_no_figures_says_nothing(self) -> None:
        self.assertEqual(self._claims({"README.md": "# Tool\n\nIt reads files.\n"}), [])

    def test_a_markdown_fixture_under_tests_is_not_a_published_figure(self) -> None:
        # Classified `test` by role, so a number inside it is something the
        # suite exercises rather than something this repository publishes.
        self.assertEqual(
            self._claims({"tests/test_fixture.md": "The run completed in 5 ms.\n"}), []
        )

    def _coverage(self, sources: dict[str, str]) -> tuple[int, int]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            for name, body in sources.items():
                (root / name).write_text(body, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            record = next(item for item in result.coverage if item.analyzer == ANALYZER_VERSION)
            return record.eligible_files, record.analyzed_files

    def test_a_document_stating_no_figure_is_not_counted_as_eligible(self) -> None:
        # Counting every documentation file put the yield at 17% and listed
        # this analyzer under "Where this analysis is thin", telling a reader
        # the section was weakly supported when nineteen of twenty-three
        # documents simply publish no figures. No claim changed, which is why
        # nothing else here would have caught it.
        eligible, analyzed = self._coverage(
            {
                "PERF.md": "The suite completed in 16 seconds.\n",
                "GUIDE.md": "# Guide\n\nRun the tool on a repository.\n",
                "NOTES.md": "# Notes\n\nNothing quantitative here at all.\n",
            }
        )
        self.assertEqual((eligible, analyzed), (1, 1))

    def test_a_long_table_states_what_it_withheld(self) -> None:
        rows = "".join(f"| step{index} | {index} ms |\n" for index in range(40))
        found = self._claims({"PERF.md": f"Measured:\n\n| a | b |\n|---|---|\n{rows}"})
        self.assertTrue(any("are not listed individually" in item for item in found))
        self.assertTrue(any("carries 40 line(s)" in item for item in found))
