# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.state import default_state_dir, resolve_state_dir


class StateDirectoryTests(TestCase):
    def test_default_state_is_stable_external_and_repository_specific(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            first = workspace / "first repo"
            second = workspace / "second repo"
            state_home = workspace / "state-home"
            first.mkdir()
            second.mkdir()

            first_state = default_state_dir(first, state_home=state_home)
            repeated = default_state_dir(first, state_home=state_home)
            second_state = default_state_dir(second, state_home=state_home)

            self.assertEqual(first_state, repeated)
            self.assertNotEqual(first_state, second_state)
            self.assertTrue(first_state.is_relative_to(state_home.resolve()))
            self.assertFalse(first_state.is_relative_to(first.resolve()))
            self.assertFalse(first_state.exists())

    def test_explicit_state_inside_target_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()

            with self.assertRaisesRegex(ValueError, "outside"):
                resolve_state_dir(root, root / ".open-skeleton")
