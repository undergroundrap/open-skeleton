# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""The gate's own decisions, which nothing else covers.

Every check this script calls is tested where it lives. The script was not,
and it is the piece most likely to be wired into someone else's loop, so a
regression here is a regression in whatever that loop decides to accept.

It earned the coverage. While it was being written it shipped a crash that
read as a rejection, and a logic error that appended to `blocking` before
assigning it -- so the entry point raised and the yield result was discarded.
Both were found by lint and by running it, not by a test, which is exactly
the gap this file closes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "turn_gate.py"
_SPEC = importlib.util.spec_from_file_location("turn_gate", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
turn_gate = importlib.util.module_from_spec(_SPEC)
sys.modules["turn_gate"] = turn_gate
_SPEC.loader.exec_module(turn_gate)


class TurnGateTests(TestCase):
    def _run(
        self,
        sources: dict[str, str],
        *,
        hum_index: list[Path] | None = None,
        required: list[str] | None = None,
        fast: bool = True,
        minimum_coverage: float = 0.95,
        minimum_yield: float = 0.0,
    ) -> int:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            for name, body in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            state = workspace / "state"
            state.mkdir()
            # `turn_gate` is loaded from `scripts/` by path, so it carries no
            # type information and the exit code arrives as Any.
            exit_code: int = turn_gate.run(
                root,
                state,
                hum_index or [],
                required or [],
                fast,
                minimum_coverage,
                minimum_yield,
            )
            return exit_code

    CLEAN = {
        "app.py": "def add(a, b):\n    return a + b\n",
        "README.md": "# Demo\n\nA module that adds.\n",
    }

    def test_a_clean_repository_passes(self) -> None:
        self.assertEqual(self._run(self.CLEAN), 0)

    def test_a_required_language_that_was_not_read_fails(self) -> None:
        # Hum needs a pre-generated index this tool never produces itself, so
        # a repository holding Hum with no index is the blindness case.
        sources = dict(self.CLEAN)
        sources["demo.hum"] = "task greet\n"
        self.assertEqual(self._run(sources, required=["Hum"]), 1)

    def test_the_same_repository_passes_when_the_language_is_not_required(self) -> None:
        sources = dict(self.CLEAN)
        sources["demo.hum"] = "task greet\n"
        self.assertEqual(self._run(sources), 0)

    def test_a_language_with_nothing_eligible_does_not_block(self) -> None:
        # `--require-language Hum` must be safe to leave in a shared config:
        # a repository with no Hum in it is not failing a Hum requirement.
        self.assertEqual(self._run(self.CLEAN, required=["Hum"]), 0)

    def test_a_yield_threshold_blocks_a_language_that_says_nothing(self) -> None:
        # Coverage and yield answer different questions, and this is the case
        # a coverage-only gate calls success: every Python file was read and
        # none of them produced a claim.
        self.assertEqual(self._run(self.CLEAN, required=["Python"], minimum_yield=0.5), 1)

    def test_yield_is_not_demanded_of_a_language_that_was_never_read(self) -> None:
        # A language with nothing read has no yield to judge, so the coverage
        # check owns that case and the yield check stays quiet. Demanding both
        # would report one absence twice.
        sources = dict(self.CLEAN)
        sources["demo.hum"] = "task greet\n"
        self.assertEqual(
            self._run(sources, required=["Hum"], minimum_coverage=0.0, minimum_yield=0.5), 0
        )

    def test_the_full_gate_also_passes_on_a_clean_repository(self) -> None:
        # Without --fast the document is built and checked against itself and
        # against the ledger, which is a different code path entirely.
        self.assertEqual(self._run(self.CLEAN, fast=False), 0)

    def test_a_state_directory_inside_the_repository_is_refused(self) -> None:
        # The engine never writes into the tree it is analyzing, and the gate
        # resolves its state through the same guard the CLI uses.
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                turn_gate.resolve_state_dir(root, root / "state")
