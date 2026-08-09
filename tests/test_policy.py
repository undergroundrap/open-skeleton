# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from unittest import TestCase

from open_skeleton.policy import ScanPolicy, classify_language, classify_role


class ScanPolicyTests(TestCase):
    def test_sensitive_and_generated_paths_are_excluded(self) -> None:
        policy = ScanPolicy(max_file_bytes=100)
        self.assertEqual(policy.directory_exclusion("node_modules"), "excluded-directory")
        self.assertEqual(policy.file_exclusion(Path(".env"), 10), "sensitive-file-name")
        self.assertEqual(policy.file_exclusion(Path(".env.production"), 10), "sensitive-file-name")
        self.assertIsNone(policy.file_exclusion(Path(".env.example"), 10))
        self.assertEqual(policy.file_exclusion(Path("secret.pem"), 10), "sensitive-file-type")
        self.assertEqual(policy.file_exclusion(Path("large.py"), 101), "oversized-file:101>100")

    def test_language_and_role_classification(self) -> None:
        self.assertEqual(classify_language(Path("thing.hum")), "Hum")
        self.assertEqual(classify_language(Path("component.tsx")), "TypeScript JSX")
        self.assertEqual(classify_role(Path("tests/test_parser.py")), "test")
        self.assertEqual(classify_role(Path("docs/design.md")), "documentation")
        self.assertEqual(classify_role(Path(".github/workflows/ci.yml")), "workflow")


class TestRoleConventionTests(TestCase):
    """Which spellings of "this is a suite" the scanner recognizes.

    Getting this wrong is expensive in both directions. A suite read as
    production code reports its fixtures as the served surface, which is the
    mistake `open-skeleton audit` exists to catch; a suite invisible as a
    suite reports every capability as reached by nothing.

    `_test.` is mandatory in Go and common in Python, `.spec.` is how Jest
    and Angular spell it, `_spec.` is RSpec's. All three were missing, so
    `handler_test.go` and `app.spec.ts` were source code.
    """

    def test_the_conventions_of_several_ecosystems_are_recognized(self) -> None:
        for path in (
            "pkg/handler_test.go",
            "src/app.spec.ts",
            "spec/models_spec.rb",
            "src/selftest.js",
            "tests/anything.mjs",
        ):
            self.assertEqual(classify_role(Path(path)), "test", path)

    def test_a_directory_saying_a_person_runs_this_outranks_the_filename(self) -> None:
        # `scripts/smoke_test.py` is named like a suite and contains none:
        # 417 lines of argparse and a hand-rolled `check` helper, from which
        # a runner collects nothing. Reading it as a suite withdrew the true
        # claim that the repository has no conventional test files, and the
        # benchmark caught that as lost recall.
        self.assertEqual(classify_role(Path("scripts/smoke_test.py")), "source")
        self.assertEqual(classify_role(Path("tools/bench_test.py")), "source")

    def test_an_explicit_test_directory_still_wins(self) -> None:
        self.assertEqual(classify_role(Path("tests/x_test.go")), "test")

    def test_a_name_merely_containing_the_word_is_not_a_suite(self) -> None:
        for path in ("src/latest.ts", "src/contest.js", "app/protest.py"):
            self.assertEqual(classify_role(Path(path)), "source", path)


class ScriptLanguageTests(TestCase):
    """An installer is often the only place a project says how it is run.

    PowerShell was Unknown, on a Windows-first engine whose own README is
    PowerShell and whose sibling repository ships `install.ps1` as its entry
    point. hum-lang carried thirteen `.ps1` files under `tools/` -- its
    release and readiness checks -- and none of them was typed, so no role
    was assigned and nothing could be said about them.

    Batch and the shell variants were missing for the same reason: nobody
    had a file of that kind in front of them at the time.
    """

    def test_powershell_is_a_language(self) -> None:
        for suffix in (".ps1", ".psm1", ".psd1"):
            self.assertEqual(classify_language(Path("install" + suffix)), "PowerShell", suffix)

    def test_batch_and_shell_variants_are_languages(self) -> None:
        self.assertEqual(classify_language(Path("run.bat")), "Batch")
        self.assertEqual(classify_language(Path("run.cmd")), "Batch")
        self.assertEqual(classify_language(Path("run.bash")), "Shell")
        self.assertEqual(classify_language(Path("run.zsh")), "Shell")

    def test_a_typed_script_gets_the_source_role(self) -> None:
        # Untyped meant roleless, and a roleless file is one nothing can say
        # anything about.
        self.assertEqual(classify_role(Path("tools/check_all.ps1")), "source")

    def test_an_unrecognized_suffix_is_still_unknown(self) -> None:
        self.assertEqual(classify_language(Path("thing.qqq")), "Unknown")
