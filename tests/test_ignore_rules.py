# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.ignore import IgnoreRules
from open_skeleton.scanner import scan_repository


def _rules(contents: str, base: str = "") -> IgnoreRules:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / ".gitignore").write_text(contents, encoding="utf-8")
        return IgnoreRules().extended(root, base)


class IgnorePatternTests(TestCase):
    def test_a_directory_pattern_matches_only_directories(self) -> None:
        rules = _rules("build/\n")
        self.assertEqual(rules.excluded_by("build", is_dir=True), "build/")
        self.assertIsNone(rules.excluded_by("build", is_dir=False))

    def test_a_character_class_is_honoured(self) -> None:
        # `[Ll]ibrary/` is how a real Unity project spells its build cache,
        # and it is the first line of that project's own ignore file.
        rules = _rules("[Ll]ibrary/\n")
        self.assertEqual(rules.excluded_by("Library", is_dir=True), "[Ll]ibrary/")
        self.assertEqual(rules.excluded_by("library", is_dir=True), "[Ll]ibrary/")
        self.assertIsNone(rules.excluded_by("Librarian", is_dir=True))

    def test_an_unanchored_pattern_matches_at_any_depth(self) -> None:
        rules = _rules("*.log\n")
        self.assertEqual(rules.excluded_by("deep/nested/run.log", is_dir=False), "*.log")

    def test_a_star_does_not_cross_a_directory_separator(self) -> None:
        rules = _rules("build/*.js\n")
        self.assertEqual(rules.excluded_by("build/app.js", is_dir=False), "build/*.js")
        self.assertIsNone(rules.excluded_by("build/nested/app.js", is_dir=False))

    def test_a_double_star_does_cross_directories(self) -> None:
        rules = _rules("build/**/*.js\n")
        self.assertEqual(rules.excluded_by("build/nested/app.js", is_dir=False), "build/**/*.js")

    def test_a_leading_slash_anchors_to_the_declaring_directory(self) -> None:
        rules = _rules("/dist\n")
        self.assertEqual(rules.excluded_by("dist", is_dir=True), "/dist")
        self.assertIsNone(rules.excluded_by("packages/web/dist", is_dir=True))

    def test_a_later_negation_re_includes(self) -> None:
        rules = _rules("*.txt\n!keep.txt\n")
        self.assertEqual(rules.excluded_by("notes.txt", is_dir=False), "*.txt")
        self.assertIsNone(rules.excluded_by("keep.txt", is_dir=False))

    def test_comments_and_blank_lines_state_nothing(self) -> None:
        self.assertFalse(_rules("# a comment\n\n   \n").declared)

    def test_a_nested_ignore_file_applies_only_below_itself(self) -> None:
        rules = _rules("dist/\n", base="packages/web")
        self.assertEqual(rules.excluded_by("packages/web/dist", is_dir=True), "dist/")
        self.assertIsNone(rules.excluded_by("packages/api/dist", is_dir=True))

    def test_an_unclosed_bracket_is_a_literal(self) -> None:
        rules = _rules("a[b\n")
        self.assertEqual(rules.excluded_by("a[b", is_dir=False), "a[b")


class IgnoreScanTests(TestCase):
    """Generated output must leave the census, and must say why it left.

    A file disappearing from an inventory without a recorded reason is the
    failure this reader could most easily cause, so the pattern responsible
    travels with the exclusion.
    """

    def test_an_ignored_directory_is_excluded_and_names_its_pattern(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".gitignore").write_text("[Ll]ibrary/\n", encoding="utf-8")
            (root / "Library").mkdir()
            (root / "Library" / "cached.cs").write_text("class Cached {}\n", encoding="utf-8")
            (root / "Assets").mkdir()
            (root / "Assets" / "Player.cs").write_text("class Player {}\n", encoding="utf-8")

            snapshot = scan_repository(root)
            paths = {item.path for item in snapshot.files}

            self.assertEqual(paths, {".gitignore", "Assets/Player.cs"})
            reason = next(item.reason for item in snapshot.exclusions if item.path == "Library/")
            self.assertEqual(reason, "gitignored:[Ll]ibrary/")

    def test_a_repository_without_an_ignore_file_is_unaffected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("x = 1\n", encoding="utf-8")

            snapshot = scan_repository(root)

            self.assertEqual({item.path for item in snapshot.files}, {"main.py"})
            self.assertFalse(
                [item for item in snapshot.exclusions if item.reason.startswith("gitignored:")]
            )

    def test_a_nested_ignore_file_is_read_where_a_monorepo_puts_it(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            web = root / "packages" / "web"
            api = root / "packages" / "api"
            # Deliberately not `dist`: that name is on the fixed exclusion
            # list, so the test would pass without reading anything.
            (web / "generated").mkdir(parents=True)
            (api / "generated").mkdir(parents=True)
            (web / ".gitignore").write_text("generated/\n", encoding="utf-8")
            (web / "generated" / "bundle.js").write_text("//\n", encoding="utf-8")
            (api / "generated" / "bundle.js").write_text("//\n", encoding="utf-8")

            snapshot = scan_repository(root)
            paths = {item.path for item in snapshot.files}

            self.assertNotIn("packages/web/generated/bundle.js", paths)
            self.assertIn("packages/api/generated/bundle.js", paths)


class GeneratedNameFallbackTests(TestCase):
    """A name guess is used only where the repository states nothing.

    Two repositories in this corpus keep a hand-written
    `build/sites-vite-plugin.ts` -- build tooling, not build output -- and a
    fixed list of directory names deleted it from the census. Where the
    repository declares its own generated directories, that declaration is
    what decides.
    """

    def _scan_with(self, ignore: str | None) -> set[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "build").mkdir()
            (root / "build" / "plugin.ts").write_text("export default {}\n", encoding="utf-8")
            if ignore is not None:
                (root / ".gitignore").write_text(ignore, encoding="utf-8")
            return {item.path for item in scan_repository(root).files}

    def test_a_silent_repository_falls_back_to_the_name(self) -> None:
        self.assertNotIn("build/plugin.ts", self._scan_with(None))

    def test_a_repository_that_declares_anything_is_believed_about_build(self) -> None:
        # The rule below says nothing about `build/`, and that silence is the
        # repository declining to call it generated.
        self.assertIn("build/plugin.ts", self._scan_with("*.log\n"))

    def test_a_repository_that_does_ignore_build_still_has_it_excluded(self) -> None:
        self.assertNotIn("build/plugin.ts", self._scan_with("build/\n"))

    def test_a_tool_owned_name_is_excluded_either_way(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "node_modules").mkdir()
            (root / "node_modules" / "index.js").write_text("//\n", encoding="utf-8")
            (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
            paths = {item.path for item in scan_repository(root).files}
            self.assertNotIn("node_modules/index.js", paths)
