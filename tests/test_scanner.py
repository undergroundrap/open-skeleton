# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

from open_skeleton.policy import ScanPolicy
from open_skeleton.scanner import scan_repository
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
            root = Path(temporary)
            blocked = root / "blocked"
            blocked.mkdir()
            original_scandir = __import__("os").scandir

            def controlled_scandir(path):
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
            root = Path(temporary)
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
