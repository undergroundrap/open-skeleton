# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Repository shapes that are legal, unusual, and rendered anyway.

Every defect found by reading generated specifications appeared in one
repository shape and was invisible in the others: a truncation needed an
eleventh capability, a module-name defect needed two projects in one tree.
The shapes here are the ones nobody generates on purpose -- an empty
directory, a file of only comments, a path carrying a character that means
something to Markdown -- and each is checked for the two failures that
matter at this level: the run crashes, or the document ends up disagreeing
with itself.

A shape that cannot be created on this platform is skipped rather than
asserted around. Windows forbids several characters in filenames that POSIX
allows, and a test that silently passes because the file was never written
is worse than one that says it did not run.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository
from open_skeleton.spec import build_spec, load_profile, render_spec_markdown
from open_skeleton.spec.coherence import check_coherence


class DocumentShapeTests(TestCase):
    def _render(self, build: Any) -> tuple[str, tuple[Any, ...]]:
        """Build a repository, analyze it, and render it. Returns the markdown."""

        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            build(root)
            snapshot = scan_repository(root)
            ledger = EvidenceLedger(workspace / "state" / "evidence.sqlite3")
            ledger.save_snapshot(snapshot)
            ledger.save_analysis(analyze_snapshot(snapshot))
            document = build_spec(ledger, load_profile())
            markdown = render_spec_markdown(document)
        return markdown, check_coherence(document, markdown)

    def _write(self, root: Path, name: str, body: str) -> bool:
        """Write a file, reporting whether this platform allows the name."""

        try:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        except (OSError, ValueError):
            return False
        return True

    def test_an_empty_repository_renders_a_coherent_document(self) -> None:
        markdown, incoherences = self._render(lambda root: None)
        self.assertIn("Technical Specification", markdown)
        self.assertEqual(incoherences, ())

    def test_a_repository_of_one_empty_file_renders(self) -> None:
        markdown, incoherences = self._render(lambda root: self._write(root, "app.py", ""))
        self.assertIn("Technical Specification", markdown)
        self.assertEqual(incoherences, ())

    def test_a_file_of_only_comments_renders(self) -> None:
        markdown, incoherences = self._render(
            lambda root: self._write(root, "app.py", "# nothing to declare\n" * 40)
        )
        self.assertIn("Technical Specification", markdown)
        self.assertEqual(incoherences, ())

    def test_a_documentation_only_repository_renders(self) -> None:
        markdown, incoherences = self._render(
            lambda root: self._write(root, "README.md", "# Title\n\nProse only.\n")
        )
        self.assertIn("Technical Specification", markdown)
        self.assertEqual(incoherences, ())

    def test_a_deeply_nested_path_renders(self) -> None:
        nested = "/".join(f"level{index}" for index in range(30)) + "/app.py"
        markdown, incoherences = self._render(
            lambda root: self._write(root, nested, "def go():\n    return 1\n")
        )
        self.assertIn("Technical Specification", markdown)
        self.assertEqual(incoherences, ())

    def test_a_non_ascii_path_and_body_render(self) -> None:
        def build(root: Path) -> None:
            if not self._write(root, "ünïcode/app.py", "def résumé():\n    return 'café'\n"):
                self.skipTest("this platform rejects the filename")

        markdown, incoherences = self._render(build)
        self.assertIn("Technical Specification", markdown)
        self.assertEqual(incoherences, ())

    def test_a_path_carrying_a_markdown_delimiter_does_not_break_a_table(self) -> None:
        # A pipe or a backtick in a path lands inside a table cell and inside
        # a code span. Both are structural characters in Markdown, and a path
        # is attacker-adjacent input in the sense that matters here: nobody
        # writing the renderer chose it.
        def build(root: Path) -> None:
            written = any(
                self._write(root, name, "def go():\n    return 1\n")
                for name in ("we`ird/app.py", "we|ird/app.py", "we]ird/app.py")
            )
            if not written:
                self.skipTest("this platform rejects every delimiter filename")

        markdown, incoherences = self._render(build)
        self.assertEqual(incoherences, ())
        self.assertIn("ird", markdown, "the awkward path should reach the document")
        # Every row of a table must have the cell count its header declares.
        # A raw delimiter inside a cell adds one and silently shifts every
        # column after it.
        width: int | None = None
        for line in markdown.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                width = None
                continue
            cells = stripped.count("|")
            if width is None:
                width = cells
            self.assertEqual(cells, width, f"ragged table row: {stripped[:80]}")

    def test_a_single_very_long_line_renders(self) -> None:
        body = "value = '" + "x" * 40_000 + "'\n"
        markdown, incoherences = self._render(lambda root: self._write(root, "app.py", body))
        self.assertIn("Technical Specification", markdown)
        self.assertEqual(incoherences, ())

    def test_a_file_that_is_not_text_is_not_read_as_text(self) -> None:
        def build(root: Path) -> None:
            (root / "blob.py").write_bytes(bytes(range(256)) * 8)

        markdown, incoherences = self._render(build)
        self.assertIn("Technical Specification", markdown)
        self.assertEqual(incoherences, ())
