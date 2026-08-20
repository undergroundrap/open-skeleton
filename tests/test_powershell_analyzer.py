# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Twenty-one PowerShell files were `Unknown` to every reader in this engine.

A repository whose install step, release process and gate are written in
PowerShell reported that it had no entry points and no public surface. These
tests hold the reader to the two things that make it safe: it must not be
fooled by its own input, and it must not confuse a helper's arguments with the
flags a user can type.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.powershell_lexical import (
    PowerShellLexicalAnalyzer,
    _blank_noise,
    declared_functions,
    parameter_blocks,
    publishes_a_module,
    throw_sites,
)
from open_skeleton.scanner import scan_repository

SCRIPT = """\
param(
  [ValidateSet('Fast', 'Exhaustive')]
  [string] $EvidenceTier = 'Fast'
)

function Resolve-Tool {
  param([string] $Name)
  if (-not $Name) { throw 'tool name is required' }
  return $Name
}

function Invoke-Gate {
  throw "failed for $EvidenceTier"
}
"""


class BlankingTests(TestCase):
    """The reader must not be fooled by comments or strings in its input."""

    def test_a_line_comment_cannot_declare_a_function(self) -> None:
        self.assertEqual(declared_functions(_blank_noise("# function Get-Thing\n")), {})

    def test_a_block_comment_cannot_declare_a_function(self) -> None:
        source = "<#\nfunction Get-Thing\n#>\nfunction Real-Thing {}\n"
        self.assertEqual(sorted(declared_functions(_blank_noise(source))), ["Real-Thing"])

    def test_a_string_containing_throw_is_not_a_throw(self) -> None:
        source = "$message = 'throw this away'\n"
        self.assertEqual(throw_sites(source, _blank_noise(source)), [])

    def test_a_here_string_is_blanked_whole(self) -> None:
        source = '$text = @"\nfunction Fake-Thing\n"@\nfunction Real-Thing {}\n'
        self.assertEqual(sorted(declared_functions(_blank_noise(source))), ["Real-Thing"])

    def test_blanking_preserves_every_line_number(self) -> None:
        # A reported line must be the line a reader opens the file to.
        source = "# comment\n'a string'\nfunction Late-Thing {}\n"
        self.assertEqual(declared_functions(_blank_noise(source))["Late-Thing"], 3)


class ParameterBlockTests(TestCase):
    def test_a_script_level_block_is_the_command_line(self) -> None:
        blocks = parameter_blocks(_blank_noise(SCRIPT))
        script = [item for item in blocks if item.script_level]
        self.assertEqual(len(script), 1)
        self.assertEqual(script[0].names, ("EvidenceTier",))

    def test_a_block_inside_a_function_is_not_the_command_line(self) -> None:
        # Reporting a helper's arguments as flags a user can type is the one
        # way this claim can be confidently wrong.
        blocks = parameter_blocks(_blank_noise(SCRIPT))
        nested = [item for item in blocks if not item.script_level]
        self.assertEqual([item.names for item in nested], [("Name",)])

    def test_a_type_and_a_validator_are_not_part_of_the_name(self) -> None:
        blocks = parameter_blocks(_blank_noise(SCRIPT))
        self.assertNotIn("Fast", blocks[0].names)

    def test_a_script_without_parameters_declares_none(self) -> None:
        self.assertEqual(parameter_blocks(_blank_noise("Write-Host 'hi'\n")), [])


class FunctionAndThrowTests(TestCase):
    def test_functions_are_found_with_their_lines(self) -> None:
        found = declared_functions(_blank_noise(SCRIPT))
        self.assertEqual(sorted(found), ["Invoke-Gate", "Resolve-Tool"])
        self.assertEqual(found["Resolve-Tool"], 6)

    def test_a_single_quoted_message_is_quoted(self) -> None:
        sites = throw_sites(SCRIPT, _blank_noise(SCRIPT))
        self.assertIn("tool name is required", [message for _, message in sites])

    def test_an_expandable_message_is_not_quoted(self) -> None:
        # PowerShell expands `$name` inside double quotes, so that text is a
        # template rather than anything a reader will see in a console.
        sites = throw_sites(SCRIPT, _blank_noise(SCRIPT))
        self.assertTrue(any(message is None for _, message in sites))
        self.assertFalse(any(message and "$" in message for _, message in sites))


class PowerShellPipelineTests(TestCase):
    def _analyze(self, name: str, body: str) -> dict[str, str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            return {item.category: item.claim for item in result.claims}

    def test_the_facts_a_script_states_reach_the_pipeline(self) -> None:
        found = self._analyze("build.ps1", SCRIPT)
        self.assertIn("`-EvidenceTier`", found["command_line_interface"])
        self.assertIn("throws in 2 place(s)", found["failure_surface"])

    def test_a_script_does_not_publish_a_public_surface(self) -> None:
        # The engine's own audit flagged this: a test harness was reported as
        # publishing thirty functions because dot-sourcing is possible.
        # Nothing dot-sources it, and across this corpus the claim was wrong
        # every time it fired -- twenty `.ps1` files and no module anywhere.
        self.assertNotIn("public_api", self._analyze("build.ps1", SCRIPT))

    def test_a_module_does_publish_one(self) -> None:
        found = self._analyze("Tools.psm1", SCRIPT)
        self.assertIn("`Invoke-Gate`", found["public_api"])
        self.assertIn("This file is a module", found["public_api"])

    def test_an_explicit_export_makes_a_script_a_module(self) -> None:
        body = SCRIPT + "\nExport-ModuleMember -Function Invoke-Gate\n"
        self.assertIn("public_api", self._analyze("build.ps1", body))

    def test_the_command_line_claim_matches_the_other_languages(self) -> None:
        # The same category Python's argparse and Rust's clap produce, so a
        # reader asks one question rather than three.
        found = self._analyze("build.ps1", SCRIPT)
        self.assertIn("These are the words a user types", found["command_line_interface"])

    def test_a_script_under_tests_is_not_the_product_surface(self) -> None:
        found = self._analyze("tests/helper.ps1", SCRIPT)
        self.assertNotIn("command_line_interface", found)
        self.assertNotIn("public_api", found)

    def test_module_detection_reads_the_suffix_and_the_export(self) -> None:
        self.assertTrue(publishes_a_module("Tools.psm1", ""))
        self.assertFalse(publishes_a_module("build.ps1", "function Go {}"))
        self.assertTrue(publishes_a_module("build.ps1", "Export-ModuleMember -Function Go"))

    def test_coverage_counts_every_eligible_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.ps1").write_text(SCRIPT, encoding="utf-8")
            (root / "b.psm1").write_text("function Only-This {}\n", encoding="utf-8")
            result = PowerShellLexicalAnalyzer().analyze(scan_repository(root))
            self.assertEqual(result.coverage[0].eligible_files, 2)
            self.assertEqual(result.coverage[0].analyzed_files, 2)
            self.assertFalse(result.coverage[0].failures)

    def test_every_claim_cites_evidence_that_exists(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.ps1").write_text(SCRIPT, encoding="utf-8")
            result = PowerShellLexicalAnalyzer().analyze(scan_repository(root))
            ids = {item.evidence_id for item in result.evidence}
            for item in result.claims:
                self.assertTrue(set(item.supporting_evidence).issubset(ids))

    def test_function_names_become_searchable(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.ps1").write_text(SCRIPT, encoding="utf-8")
            result = PowerShellLexicalAnalyzer().analyze(scan_repository(root))
            index: dict[str, int] = {}
            for symbol in result.symbols:
                index.update(symbol.metadata.get("name_index", {}))
            self.assertIn("Resolve-Tool", index)
            self.assertIn("EvidenceTier", index)
