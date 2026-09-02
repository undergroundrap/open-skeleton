# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""What a question costs to ask.

A rendered specification is a document for people. For an agent it is a bill:
this engine's own `spec.md` runs to roughly 34,700 words, `spec.json` 78,600,
and `spec.index.json` 124,200. The query commands exist so that learning one
fact does not cost what learning everything costs.

Nothing else in this suite would notice that changing. A command that starts
printing the whole ledger still returns correct data, still passes its own
tests, and quietly stops being usable by the thing it was written for. These
ceilings are deliberately loose -- they are not style rules, they are the
difference between a query and a download.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.cli import main
from tests.helpers import create_sample_repository

# Generous by design. The point is to catch a command that starts emitting the
# repository rather than an answer, not to police wording.
QUERY_WORD_CEILING = 1_500


class QueryBudgetTests(TestCase):
    def _run(self, *argv: str) -> str:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            state = Path(temporary) / "state"
            root.mkdir()
            create_sample_repository(root)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                main(["analyze", str(root), "--state-dir", str(state)])
            captured = StringIO()
            with redirect_stdout(captured), redirect_stderr(StringIO()):
                main([argv[0], str(root), "--state-dir", str(state), *argv[1:]])
            return captured.getvalue()

    def _assert_bounded(self, label: str, output: str) -> None:
        words = len(output.split())
        self.assertLess(
            words,
            QUERY_WORD_CEILING,
            f"{label} returned {words:,} words; a query that costs this much is a download",
        )

    def test_status_is_a_query(self) -> None:
        self._assert_bounded("status", self._run("status"))

    def test_contracts_is_a_query(self) -> None:
        self._assert_bounded("contracts", self._run("contracts"))

    def test_contracts_json_is_a_query(self) -> None:
        self._assert_bounded("contracts --json", self._run("contracts", "--json"))

    def test_audit_is_a_query(self) -> None:
        self._assert_bounded("audit", self._run("audit"))

    def test_a_narrowed_question_costs_less_than_an_open_one(self) -> None:
        """The property that makes the surface worth having.

        Measured on a repository that actually declares two contracts. The
        first version of this test used a fixture with none, so both answers
        were "nothing found" and the narrowed one was *longer* -- it echoes
        the term back. A comparison between two empty answers proves nothing.
        """

        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            state = Path(temporary) / "state"
            root.mkdir()
            (root / "store.py").write_text(
                'SCHEMA = "CREATE TABLE job ('
                "state TEXT NOT NULL CHECK (state IN ('queued', 'done')), "
                "mode TEXT NOT NULL CHECK (mode IN ('fast', 'slow'))"
                ');"\n',
                encoding="utf-8",
            )
            (root / "guard.py").write_text(
                "def check(state, mode):\n"
                "    if state not in {'queued', 'done'}:\n"
                "        raise ValueError(state)\n"
                "    if mode not in {'fast', 'slow'}:\n"
                "        raise ValueError(mode)\n",
                encoding="utf-8",
            )
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                main(["analyze", str(root), "--state-dir", str(state)])

            def ask(*extra: str) -> int:
                captured = StringIO()
                with redirect_stdout(captured), redirect_stderr(StringIO()):
                    main(["contracts", str(root), "--state-dir", str(state), *extra])
                return len(captured.getvalue().split())

            everything = ask()
            narrowed = ask("--term", "queued")
            self.assertGreater(everything, 0)
            self.assertLess(narrowed, everything)

    def test_an_empty_answer_still_says_something(self) -> None:
        # Silence reads as breakage. A caller has to be able to tell "no such
        # contract" apart from "the command did not run".
        output = self._run("contracts", "--term", "nothing-declares-this")
        self.assertTrue(output.strip())
