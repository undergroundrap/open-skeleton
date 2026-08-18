# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

from open_skeleton.models import Snapshot
from open_skeleton.policy import ScanPolicy
from open_skeleton.scanner import dropped_file_count, scan_repository
from tests.helpers import create_sample_repository


class RepositoryScannerTests(TestCase):
    def test_scan_is_deterministic_and_excludes_untrusted_inputs(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_sample_repository(root)

            first = scan_repository(root)
            second = scan_repository(root)

            self.assertEqual(first.snapshot_id, second.snapshot_id)
            self.assertEqual(len(first.files), 5)
            self.assertEqual(
                [item.path for item in first.files],
                ["README.md", "package.json", "src/app.py", "tests/test_app.py", "web/app.ts"],
            )
            exclusion_reasons = {item.path: item.reason for item in first.exclusions}
            self.assertEqual(exclusion_reasons[".env"], "sensitive-file-name")
            self.assertEqual(exclusion_reasons["node_modules/"], "excluded-directory")
            self.assertEqual(exclusion_reasons["payload.dat"], "binary-content")
            self.assertEqual(first.events[-1].stage, "complete")

    def test_content_change_produces_new_snapshot(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_sample_repository(root)
            before = scan_repository(root)

            (root / "src" / "app.py").write_text(
                "def answer() -> int:\n    return 43\n",
                encoding="utf-8",
            )
            after = scan_repository(root)

            self.assertNotEqual(before.snapshot_id, after.snapshot_id)

    def test_maximum_file_size_is_enforced_before_reading(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "large.py").write_text("x" * 101, encoding="utf-8")
            snapshot = scan_repository(root, policy=ScanPolicy(max_file_bytes=100))
            self.assertEqual(snapshot.files, ())
            self.assertEqual(snapshot.exclusions[0].reason, "oversized-file:101>100")

    def test_malformed_utf8_is_excluded_without_partial_ingestion(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "invalid.py").write_bytes(b"value = '\xff'\n")

            snapshot = scan_repository(root)

            self.assertEqual(snapshot.files, ())
            self.assertEqual(snapshot.exclusions[0].reason, "unsupported-text-encoding")

    def test_unreadable_directory_is_recorded_not_fatal(self) -> None:
        with TemporaryDirectory() as temporary:
            # scan_repository resolves its root, so a fixture that compares
            # against an unresolved temporary directory never matches where the
            # two spellings differ: Windows hands back an 8.3 short name
            # (RUNNER~1 for runneradmin) and macOS symlinks /var to /private/var.
            root = Path(temporary).resolve()
            blocked = root / "blocked"
            blocked.mkdir()
            original_scandir = __import__("os").scandir

            def controlled_scandir(path: str | Path) -> object:
                if Path(path) == blocked:
                    raise PermissionError("fixture")
                return original_scandir(path)

            with patch("open_skeleton.scanner.os.scandir", side_effect=controlled_scandir):
                snapshot = scan_repository(root)

            self.assertEqual(snapshot.exclusions[0].path, "blocked")
            self.assertEqual(
                snapshot.exclusions[0].reason,
                "directory-read-error:PermissionError",
            )

    def test_symlink_is_never_followed(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            outside = workspace / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "secret.py").write_text("secret = True\n", encoding="utf-8")
            link = root / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            snapshot = scan_repository(root)

            self.assertEqual(snapshot.files, ())
            self.assertEqual(snapshot.exclusions[0].path, "linked")
            self.assertEqual(snapshot.exclusions[0].reason, "symlink-not-followed")

    def test_symlink_branch_is_enforced_without_platform_privileges(self) -> None:
        with TemporaryDirectory() as temporary:
            # The fabricated entry has to carry the path the scanner will see,
            # which is resolved. An unresolved temporary directory differs from
            # it on Windows (8.3 short names) and macOS (/var is a symlink).
            root = Path(temporary).resolve()
            entry = Mock()
            entry.name = "linked"
            entry.path = str(root / "linked")
            entry.is_symlink.return_value = True
            iterator = MagicMock()
            iterator.__enter__.return_value = iter([entry])
            iterator.__exit__.return_value = False

            with patch("open_skeleton.scanner.os.scandir", return_value=iterator):
                snapshot = scan_repository(root)

            entry.is_dir.assert_not_called()
            entry.is_file.assert_not_called()
            self.assertEqual(snapshot.files, ())
            self.assertEqual(snapshot.exclusions[0].path, "linked")
            self.assertEqual(snapshot.exclusions[0].reason, "symlink-not-followed")


class GeneratedDirectoryTests(TestCase):
    """Build output carries the package name, so no fixed set can exclude it.

    Found by comparing the file census against another tool's reading of the
    same repository. It counted 107 files and this one counted 116, and six of
    the difference were `src/open_skeleton.egg-info/` -- generated by the build
    and tracked by nothing, yet read as source and offered to every analyzer as
    if a person had written it.

    `dist`, `build` and `target` were already excluded by name. An egg-info
    directory cannot be, because its name begins with whatever the package is
    called.
    """

    def _paths(self, *relative: str) -> set[str]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for item in relative:
                target = root / item
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("value = 1\n", encoding="utf-8")
            return {record.path for record in scan_repository(root).files}

    def test_an_egg_info_directory_is_not_source(self) -> None:
        found = self._paths("pkg/real.py", "pkg.egg-info/SOURCES.txt")
        self.assertEqual(found, {"pkg/real.py"})

    def test_a_dist_info_directory_is_not_source(self) -> None:
        found = self._paths("real.py", "thing-1.0.dist-info/METADATA")
        self.assertEqual(found, {"real.py"})

    def test_a_directory_merely_containing_the_word_is_kept(self) -> None:
        # `egg-info-utils` is a normal package name, not build output.
        found = self._paths("egg-info-utils/helper.py")
        self.assertEqual(found, {"egg-info-utils/helper.py"})


class ExcludedFileCountTests(TestCase):
    """An excluded directory must say how much it took with it.

    A census that counts rows where it means files overstates its own
    coverage, which is the one thing the exclusions panel exists to prevent.
    """

    def _snapshot(self) -> Snapshot:
        root = Path(self.temporary.name)
        (root / ".gitignore").write_text("cache/\n", encoding="utf-8")
        nested = root / "cache" / "deep"
        nested.mkdir(parents=True)
        for index in range(3):
            (root / "cache" / f"a{index}.txt").write_text("x\n", encoding="utf-8")
        (nested / "b.txt").write_text("x\n", encoding="utf-8")
        (root / "main.py").write_text("x = 1\n", encoding="utf-8")
        return scan_repository(root)

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def test_the_directory_row_counts_files_at_every_depth(self) -> None:
        snapshot = self._snapshot()
        row = next(item for item in snapshot.exclusions if item.path == "cache/")
        self.assertEqual(row.contained_files, 4)

    def test_a_single_excluded_file_counts_as_itself(self) -> None:
        snapshot = self._snapshot()
        rows = [item for item in snapshot.exclusions if item.path != "cache/"]
        self.assertTrue(all(item.contained_files == 0 for item in rows))
        self.assertEqual(dropped_file_count(list(snapshot.exclusions)), 4 + len(rows))

    def test_the_total_is_not_the_number_of_rows(self) -> None:
        snapshot = self._snapshot()
        self.assertGreater(dropped_file_count(list(snapshot.exclusions)), len(snapshot.exclusions))
