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

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.powershell_lexical import (
    PowerShellLexicalAnalyzer,
    _blank_noise,
    declared_functions,
    declared_shapes,
    declared_value_sets,
    declared_values,
    environment_reads,
    imported_modules,
    parameter_blocks,
    publishes_a_module,
    test_blocks,
    throw_sites,
)
from open_skeleton.scanner import scan_repository


def _test_blocks(source: str) -> list[tuple[str, int]]:
    return test_blocks(source, _blank_noise(source))


def _imports(source: str) -> list[tuple[str, int]]:
    return imported_modules(source, _blank_noise(source))


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


class PowerShellDeclaredValueTests(TestCase):
    """Written against what PowerShell code does, not what the language allows.

    The conformance snippet for this reader first used `Set-Variable -Option
    Constant` and an `enum`. `PSDesiredStateConfiguration`, which ships with
    Windows, contains zero of either across 25 files: it states limits as
    `$script:MaxComponentDepth = 1024` and vocabularies as `ValidateSet`.
    Building for the manual would have passed my own test and read nothing.
    """

    SOURCE = (
        "$script:MaxComponentDepth = 1024\n"
        '$script:PsDscCompatibleVersion = "2.0.0"\n'
        "$script:Cache = @{}\n"
        "# $script:Commented = 5\n"
        "function Set-Thing {\n"
        "    param(\n"
        '        [ValidateSet("Present", "Absent")]\n'
        "        [String] $Ensure,\n"
        '        [ValidateSet("", "SHA-1", "SHA-256")]\n'
        "        [String] $Checksum\n"
        "    )\n"
        "}\n"
    )

    def _values(self) -> dict[str, Any]:
        return declared_values(self.SOURCE, _blank_noise(self.SOURCE))

    def test_a_numeric_limit_is_recorded(self) -> None:
        self.assertEqual(self._values()["MaxComponentDepth"]["value"], "1024")

    def test_a_string_value_survives_the_blanking(self) -> None:
        # Blanking removes a string body and its quotes, so the value comes
        # from the original at the same offsets. Trimming the match on the
        # blanked text left the group holding a lone quote.
        self.assertEqual(self._values()["PsDscCompatibleVersion"]["value"], "2.0.0")

    def test_a_computed_value_is_not_recorded(self) -> None:
        # `@{}` is a hashtable this reader would have to run a shell to know.
        self.assertNotIn("Cache", self._values())

    def test_a_variable_in_a_comment_is_not_a_declaration(self) -> None:
        self.assertNotIn("Commented", self._values())

    def test_validate_set_is_a_vocabulary_named_for_its_parameter(self) -> None:
        found = declared_value_sets(self.SOURCE, _blank_noise(self.SOURCE))
        self.assertEqual(found["Ensure"]["members"], ["Present", "Absent"])

    def test_an_empty_member_is_not_a_value(self) -> None:
        found = declared_value_sets(self.SOURCE, _blank_noise(self.SOURCE))
        self.assertEqual(found["Checksum"]["members"], ["SHA-1", "SHA-256"])


class PowerShellImportTests(TestCase):
    """What a script loads, in the spellings Microsoft's own modules use.

    Across the 155 PowerShell files Windows ships there are 59 `Import-Module`
    lines, 10 dot-sources, and zero uses of either `using module` or
    `#Requires -Modules`. Every case here is one of those lines; a reader
    written from the language reference would have implemented the two forms
    that never appear and missed all of the ones that do.
    """

    def test_a_bare_module_name_is_a_target(self) -> None:
        self.assertEqual(_imports("Import-Module Hyper-V\n"), [("Hyper-V", 1)])

    def test_the_name_parameter_is_skipped(self) -> None:
        self.assertEqual(_imports("Import-Module -Name Pester\n"), [("Pester", 1)])

    def test_a_quoted_script_root_path_survives_the_blanking(self) -> None:
        # The most common form in that corpus, and the one blanking empties:
        # the target has to come from the source at the match's offsets.
        self.assertEqual(
            _imports('import-module "$PSScriptRoot\\CmdletHelpers.psm1" -Force\n'),
            [("$PSScriptRoot\\CmdletHelpers.psm1", 1)],
        )

    def test_a_relative_path_is_a_target(self) -> None:
        self.assertEqual(
            _imports("import-module Storage\\StorageHealth.cdxml\n"),
            [("Storage\\StorageHealth.cdxml", 1)],
        )

    def test_a_script_block_import_closes_cleanly(self) -> None:
        self.assertEqual(
            _imports("Invoke-Command -ScriptBlock {Import-Module msdtc}\n"), [("msdtc", 1)]
        )

    def test_a_piped_import_names_nothing(self) -> None:
        self.assertEqual(_imports("Get-Thing | Import-Module -Force\n"), [])

    def test_a_module_held_in_a_variable_is_not_named(self) -> None:
        # `Import-Module -Name $interopdll` names a value this reader would
        # have to run the shell to learn.
        self.assertEqual(_imports("Import-Module -Name $interopdll\n"), [])

    def test_an_import_in_prose_is_not_an_import(self) -> None:
        # Pester's own help text contains this sentence.
        self.assertEqual(_imports("# the consumer might have to import-module Foo\n"), [])

    def test_an_import_in_a_here_string_is_not_an_import(self) -> None:
        self.assertEqual(_imports('@"\nImport-Module Foo\n"@\n'), [])

    def test_a_dot_source_loads_a_script(self) -> None:
        self.assertEqual(_imports('. "$here\\Add-Numbers.ps1"\n'), [("$here\\Add-Numbers.ps1", 1)])

    def test_a_dot_source_of_a_command_is_not_a_load(self) -> None:
        self.assertEqual(_imports(". AdvancedFunction\n"), [])

    def test_a_range_and_a_method_call_are_not_dot_sources(self) -> None:
        self.assertEqual(_imports("$a = 1..5\n    .Invoke()\n"), [])

    def test_imports_become_edges(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "build.ps1").write_text("Import-Module Pester\n", encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
        edges = [edge for edge in result.edges if edge.relationship == "imports"]
        self.assertEqual([edge.target_ref for edge in edges], ["Pester"])


class PowerShellEnvironmentTests(TestCase):
    """What a script needs from its environment, and what it changes.

    Measured before it was written. There are 148 `$env:` uses across the 155
    PowerShell files Windows ships; blanking erases 75 of them, because
    PowerShell expands a double-quoted string and `"$env:windir\\system32"` is
    a real read. Not one of the 148 sits inside a single-quoted string, where
    it would be literal text.
    """

    def test_a_bare_read_is_recorded(self) -> None:
        self.assertEqual(
            environment_reads("$x = $env:SERVICE_URL\n"), [("SERVICE_URL", "reads", 1)]
        )

    def test_an_expandable_string_is_a_read(self) -> None:
        # Half the reads in that corpus are this shape.
        self.assertEqual(
            environment_reads('$p = "$env:windir' + chr(92) + 'system32"\n'),
            [("windir", "reads", 1)],
        )

    def test_a_literal_string_is_not_a_read(self) -> None:
        self.assertEqual(environment_reads("$x = '$env:SERVICE_URL'\n"), [])

    def test_a_comment_is_not_a_read(self) -> None:
        self.assertEqual(environment_reads("# add $scopePath to $env:Path\n"), [])

    def test_an_assignment_is_reported_as_a_write(self) -> None:
        # Twelve of these in that corpus, and each changes what every child
        # process inherits.
        self.assertEqual(
            environment_reads("$env:PSModulePath = 'x'\n"), [("PSModulePath", "sets", 1)]
        )

    def test_a_comparison_is_not_a_write(self) -> None:
        self.assertEqual(environment_reads("if ($env:PATH -eq 'x') { }\n"), [("PATH", "reads", 1)])

    def test_the_braced_form_is_read(self) -> None:
        self.assertEqual(environment_reads("${env:Braced}\n"), [("Braced", "reads", 1)])

    def test_two_settings_on_one_line_are_both_recorded(self) -> None:
        # The pattern once captured the text that told a read from a write,
        # and `finditer` does not overlap, so the second went missing.
        source = '$p = "$env:SystemDrive' + chr(92) + '$env:HOMEPATH"\n'
        self.assertEqual(
            [name for name, _, _ in environment_reads(source)], ["SystemDrive", "HOMEPATH"]
        )

    def test_a_read_becomes_a_claim(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "build.ps1").write_text("$x = $env:SERVICE_URL\n", encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
        claims = [item for item in result.claims if item.category == "configuration_read"]
        self.assertEqual(len(claims), 1)
        self.assertIn("SERVICE_URL", claims[0].claim)


class PowerShellShapeTests(TestCase):
    """What a PowerShell class holds.

    Fifteen classes across the 155 files Windows ships, which sounds thin
    until one is opened: `DOSwarmStats` declares eighteen typed properties and
    `StorageHistory` forty-six, every one written `[uint64] $BytesFromPeers`.
    Checked against those files, read as bytes the way the pipeline reads
    them: 15 classes found of 15 declared, 301 fields.
    """

    def test_a_typed_property_is_a_field(self) -> None:
        found = declared_shapes(
            _blank_noise("class DOPeerInfo {\n    [string] $IP;\n    [uint64] $BytesSent\n}\n")
        )
        self.assertEqual(
            [(item["name"], item["annotation"]) for item in found["DOPeerInfo"]["fields"]],
            [("IP", "string"), ("BytesSent", "uint64")],
        )

    def test_a_base_class_is_recorded(self) -> None:
        found = declared_shapes(
            _blank_noise("class DOExtendedConfig : DOConfig {\n    [int] $Depth = 4\n}\n")
        )
        self.assertEqual(found["DOExtendedConfig"]["bases"], ["DOConfig"])

    def test_a_parameter_is_not_a_field(self) -> None:
        # A parameter is spelled exactly like a property. What separates them
        # is the brace it sits inside, not the shape of the line.
        source = (
            "function Set-Thing {\n"
            "    param(\n"
            "        [ValidateSet('GET','PUT')]\n"
            "        [String] $Method\n"
            "    )\n"
            "}\n"
        )
        self.assertEqual(declared_shapes(_blank_noise(source)), {})

    def test_a_property_may_span_two_lines(self) -> None:
        # `VMDirectStorage` writes every property with its type above its
        # name. Requiring one line dropped that whole class, and fourteen of
        # fifteen looks like success until you ask which one is missing.
        found = declared_shapes(_blank_noise("class VMDisk {\n    [String]\n    $UniqueId\n}\n"))
        self.assertEqual([item["name"] for item in found["VMDisk"]["fields"]], ["UniqueId"])

    def test_windows_line_endings_are_read(self) -> None:
        # `[ \t]*$` matches at the end of a line, and on a CRLF file the
        # character in that position is `\r`. Every property in every file
        # saved with Windows line endings failed silently, and the corpus
        # check missed it because `read_text` had already translated them.
        source = "class Order {\r\n    [string] $Identifier\r\n}\r\n"
        found = declared_shapes(_blank_noise(source))
        self.assertEqual([item["name"] for item in found["Order"]["fields"]], ["Identifier"])

    def test_shapes_reach_the_module_symbol(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "types.ps1").write_text(
                "class Order {\n    [string] $Identifier\n}\n", encoding="utf-8"
            )
            result = analyze_snapshot(scan_repository(root))
        models: dict[str, Any] = {}
        for symbol in result.symbols:
            metadata = symbol.metadata or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            models.update(metadata.get("model_fields") or {})
        self.assertIn("Order", models)


class PowerShellTestDeclarationTests(TestCase):
    """How many tests a PowerShell repository declares.

    Pester is what the corpus uses: 483 `It`, 162 `Describe` and 79 `Context`
    blocks across the 155 files Windows ships. Reading it back with the same
    bytes the pipeline reads: 493 tests in 226 groups across 40 files.
    """

    def test_an_it_block_is_a_test(self) -> None:
        found = _test_blocks("Describe 'Thing' {\n    It 'works' { }\n}\n")
        self.assertEqual(found, [("describe", 1), ("it", 2)])

    def test_a_function_definition_is_not_a_block(self) -> None:
        # Pester's own source defines all three keywords, once each.
        self.assertEqual(_test_blocks("function It { param($name) }\n"), [])

    def test_an_assignment_is_not_a_block(self) -> None:
        # `Describe = $CurrentDescribe` is a hashtable key in Pester's source.
        self.assertEqual(_test_blocks("Describe               = $CurrentDescribe\n"), [])

    def test_a_longer_word_is_not_a_block(self) -> None:
        self.assertEqual(_test_blocks("Itemize 'not a block' { }\n"), [])

    def test_a_block_in_comment_based_help_is_not_a_block(self) -> None:
        self.assertEqual(_test_blocks("<#\nDescribe 'in help' {\n#>\n"), [])

    def test_windows_line_endings_are_read(self) -> None:
        # This reader made the same mistake once already, and the standing
        # check named this pattern the first time the tests ran after it was
        # written. Every Pester block in every CRLF file went unread.
        found = _test_blocks("Describe 'Thing' {\r\n    It 'works' { }\r\n}\r\n")
        self.assertEqual([keyword for keyword, _ in found], ["describe", "it"])

    def test_tests_become_a_claim(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Thing.Tests.ps1").write_text(
                "Describe 'Thing' {\n    It 'works' { }\n    It 'also works' { }\n}\n",
                encoding="utf-8",
            )
            result = analyze_snapshot(scan_repository(root))
        claims = [item.claim for item in result.claims if item.category == "testing"]
        self.assertEqual(len(claims), 1)
        self.assertIn("2 Pester test(s) in 1 group(s)", claims[0])
