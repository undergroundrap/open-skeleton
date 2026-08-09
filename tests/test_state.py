# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import _dependency_name
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


class DependencyNameTests(TestCase):
    """A module specifier that names no package is not a missing dependency.

    skill-cue's document opened with four contradictions between sources, and
    three were manufactured. `import { skillCards } from
    "../../src/lib/skillCards"` was reported as "Operator scripts import /",
    because the specifier was split on `.` -- Python's separator -- which
    turns a relative JavaScript path into a slash. `node:fs` and `node:path`
    were reported as undeclared, and a runtime's own standard library never
    appears in a manifest.

    These carried `conflict` status, the most severe this engine assigns. A
    false conflict is worse than a missed one: it is the finding a reader
    acts on first.
    """

    def test_a_relative_specifier_names_no_package(self) -> None:
        for specifier in ("../../src/lib/skillCards", "./local", "/abs", "#internal"):
            self.assertIsNone(_dependency_name(specifier), specifier)

    def test_a_runtime_builtin_names_no_package(self) -> None:
        for specifier in ("node:fs", "node:path", "fs", "path"):
            self.assertIsNone(_dependency_name(specifier), specifier)

    def test_a_scoped_package_keeps_both_segments(self) -> None:
        self.assertEqual(_dependency_name("@scope/pkg"), "@scope/pkg")
        self.assertEqual(_dependency_name("@scope/pkg/deep"), "@scope/pkg")
        self.assertIsNone(_dependency_name("@scope"))

    def test_a_subpath_import_names_its_package(self) -> None:
        self.assertEqual(_dependency_name("lodash/merge"), "lodash")

    def test_a_python_submodule_names_its_distribution(self) -> None:
        self.assertEqual(_dependency_name("os.path"), "os")
        self.assertEqual(_dependency_name("requests"), "requests")
