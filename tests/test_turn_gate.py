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
import io
import shlex
import sys
from contextlib import redirect_stdout
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

    def test_a_generator_that_fails_but_writes_a_graph_is_believed(self) -> None:
        """A compiler exits non-zero on a corpus that carries errors on purpose.

        `hum graph` over hum-lang exits 1 and writes a complete 2.26 MB graph
        of 229 files, because 24 of its `.hum` files are malformed deliberately
        so a diagnostic fires. Reading that status as a broken generator failed
        the gate on every turn of the repository it was built for, and said so
        in words blaming the generator.
        """

        with TemporaryDirectory() as temporary:
            index = Path(temporary) / "graph.json"
            index.write_text(
                '{"schema": "hum.semantic_graph.v0", "summary": {}, "files": []}',
                encoding="utf-8",
            )
            self.assertTrue(turn_gate._usable_indexes([index]))

    def test_a_generator_that_writes_nothing_usable_is_not(self) -> None:
        with TemporaryDirectory() as temporary:
            missing = Path(temporary) / "absent.json"
            self.assertFalse(turn_gate._usable_indexes([missing]))
            truncated = Path(temporary) / "half.json"
            truncated.write_text('{"schema": "hum.semantic', encoding="utf-8")
            self.assertFalse(turn_gate._usable_indexes([truncated]))
            other = Path(temporary) / "other.json"
            other.write_text('{"schema": "something.else.v1"}', encoding="utf-8")
            self.assertFalse(turn_gate._usable_indexes([other]))

    def test_an_undeclared_index_leaves_only_the_exit_code(self) -> None:
        # With nothing declared there is no output to inspect, so a failing
        # generator must still fail rather than be assumed fine.
        self.assertFalse(turn_gate._usable_indexes([]))

    def _gate_with_generator(self, exit_code: int, write_index: bool) -> tuple[int, str]:
        """Run the entry point with a generator that exits how we say.

        Returns the status and what was printed. The status alone cannot
        answer this: the gate legitimately returns 1 for an audit finding, so
        asserting on it tested something other than the decision under test.
        """

        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            (root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (root / "demo.hum").write_text("task greet\n", encoding="utf-8")
            index = workspace / "graph.json"
            if write_index:
                index.write_text(
                    '{"schema": "hum.semantic_graph.v0", "summary": {}, "files": []}',
                    encoding="utf-8",
                )
            generator = workspace / "generator.py"
            generator.write_text(f"import sys\nsys.exit({exit_code})\n", encoding="utf-8")
            argv = [
                "turn_gate.py",
                "--repo",
                str(root),
                "--state",
                str(workspace / "state"),
                "--hum-index",
                str(index),
                "--fast",
                "--hum-graph-command",
                f"{shlex.quote(sys.executable)} {shlex.quote(str(generator))}",
            ]
            original = sys.argv
            sys.argv = argv
            captured = io.StringIO()
            try:
                with redirect_stdout(captured):
                    code = int(turn_gate.main())
            finally:
                sys.argv = original
            return code, captured.getvalue()

    REJECTED = "the generator failed"

    def test_the_entry_point_believes_a_graph_over_an_exit_code(self) -> None:
        # The helper above is tested in isolation, and that was not enough:
        # removing the guard at the call site left every one of those cases
        # green. This drives the decision the gate actually makes.
        _, output = self._gate_with_generator(1, write_index=True)
        self.assertNotIn(self.REJECTED, output)

    def test_the_entry_point_still_fails_when_nothing_was_written(self) -> None:
        code, output = self._gate_with_generator(1, write_index=False)
        self.assertIn(self.REJECTED, output)
        self.assertEqual(code, 1)

    def test_a_state_directory_inside_the_repository_is_refused(self) -> None:
        # The engine never writes into the tree it is analyzing, and the gate
        # resolves its state through the same guard the CLI uses.
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                turn_gate.resolve_state_dir(root, root / "state")
