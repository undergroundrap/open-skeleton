# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Fifteen of sixteen C# files in this corpus were read by nothing.

C# is most of .NET and all of Unity scripting, and a repository written in it
reported no public API at all. These tests hold the reader to the property
that makes a lexical analyzer safe: it must not be fooled by its own text.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.csharp_lexical import (
    CSharpLexicalAnalyzer,
    blank_noise,
    declared_constants,
    declared_enums,
    declared_namespace,
    declared_types,
    imported_namespaces,
    public_members,
    throw_sites,
)
from open_skeleton.scanner import scan_repository

SOURCE = """\
// public class NotReal
using UnityEngine;
using System.Collections.Generic;

namespace WarpWrit
{
    public sealed class Creature : MonoBehaviour
    {
        private int _health;

        public string CreatureName { get; private set; }

        public void Initialize(string name)
        {
            if (name == null)
            {
                throw new ArgumentNullException("name is required");
            }
            _health = 10;
        }

        public int Health => _health;
    }

    internal struct Marker
    {
    }
}
"""


class BlankingTests(TestCase):
    def test_a_line_comment_declares_nothing(self) -> None:
        self.assertNotIn("NotReal", {name for _, _, name, _ in declared_types(blank_noise(SOURCE))})

    def test_a_block_comment_declares_nothing(self) -> None:
        source = "/*\npublic class Ghost {}\n*/\npublic class Real {}\n"
        names = {name for _, _, name, _ in declared_types(blank_noise(source))}
        self.assertEqual(names, {"Real"})

    def test_a_string_containing_throw_is_not_a_throw(self) -> None:
        source = 'var message = "throw this away";\n'
        self.assertEqual(throw_sites(source, blank_noise(source)), [])

    def test_a_verbatim_string_is_blanked_whole(self) -> None:
        source = 'var path = @"C:\\public class Fake";\npublic class Real {}\n'
        names = {name for _, _, name, _ in declared_types(blank_noise(source))}
        self.assertEqual(names, {"Real"})

    def test_blanking_preserves_every_line_number(self) -> None:
        source = '// comment\nvar s = "text";\npublic class Late {}\n'
        types = declared_types(blank_noise(source))
        self.assertEqual(types[0][3], 3)


class DeclarationTests(TestCase):
    def setUp(self) -> None:
        self.clean = blank_noise(SOURCE)

    def test_the_namespace_is_read(self) -> None:
        self.assertEqual(declared_namespace(self.clean), "WarpWrit")

    def test_using_directives_become_imports(self) -> None:
        self.assertEqual(
            sorted(imported_namespaces(self.clean)),
            ["System.Collections.Generic", "UnityEngine"],
        )

    def test_types_are_read_with_their_access(self) -> None:
        found = {name: (access, kind) for access, kind, name, _ in declared_types(self.clean)}
        self.assertEqual(found["Creature"], ("public", "class"))
        self.assertEqual(found["Marker"], ("internal", "struct"))

    def test_a_type_declaration_is_not_read_as_a_member(self) -> None:
        # `public sealed class Creature` would otherwise read as a member
        # named `Creature` whose type is `class`.
        self.assertNotIn("Creature", {name for _, name, _ in public_members(self.clean)})

    def test_methods_and_properties_are_both_public_surface(self) -> None:
        found = {name: kind for kind, name, _ in public_members(self.clean)}
        self.assertEqual(found["Initialize"], "method")
        self.assertEqual(found["CreatureName"], "property")

    def test_an_expression_bodied_property_is_surface_too(self) -> None:
        self.assertIn("Health", {name for _, name, _ in public_members(self.clean)})

    def test_a_private_field_is_not_public_surface(self) -> None:
        self.assertNotIn("_health", {name for _, name, _ in public_members(self.clean)})


class ThrowTests(TestCase):
    def test_the_exception_type_and_message_are_read(self) -> None:
        sites = throw_sites(SOURCE, blank_noise(SOURCE))
        self.assertEqual(sites[0][0], "ArgumentNullException")
        self.assertEqual(sites[0][1], "name is required")

    def test_an_interpolated_message_is_not_quoted(self) -> None:
        # `$"..."` has no fixed text, and a reader will search for the words
        # this document gives them.
        source = 'throw new Exception($"bad {value}");\n'
        sites = throw_sites(source, blank_noise(source))
        self.assertEqual(sites[0][0], "Exception")
        self.assertIsNone(sites[0][1])

    def test_a_rethrow_carries_neither_type_nor_message(self) -> None:
        source = "try { Go(); } catch { throw; }\n"
        self.assertEqual(throw_sites(source, blank_noise(source)), [(None, None, 1)])


class CSharpPipelineTests(TestCase):
    def _claims(self, name: str = "Creature.cs") -> dict[str, str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(SOURCE, encoding="utf-8")
            result = analyze_snapshot(scan_repository(root))
            return {item.category: item.claim for item in result.claims}

    def test_public_surface_and_failures_reach_the_pipeline(self) -> None:
        found = self._claims()
        self.assertIn("`Creature`", found["public_api"])
        self.assertIn("3 public member(s)", found["public_api"])
        self.assertIn("ArgumentNullException", found["failure_surface"])
        self.assertIn('"name is required"', found["failure_surface"])

    def test_a_test_file_is_not_the_product_surface(self) -> None:
        found = self._claims("Tests/CreatureTests.cs")
        self.assertNotIn("public_api", found)

    def test_imports_become_edges(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Creature.cs").write_text(SOURCE, encoding="utf-8")
            result = CSharpLexicalAnalyzer().analyze(scan_repository(root))
            targets = {edge.target_ref for edge in result.edges if edge.relationship == "imports"}
            self.assertEqual(targets, {"UnityEngine", "System.Collections.Generic"})

    def test_every_claim_cites_evidence_that_exists(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Creature.cs").write_text(SOURCE, encoding="utf-8")
            result = CSharpLexicalAnalyzer().analyze(scan_repository(root))
            ids = {item.evidence_id for item in result.evidence}
            for item in result.claims:
                self.assertTrue(set(item.supporting_evidence).issubset(ids))

    def test_declared_names_become_searchable(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Creature.cs").write_text(SOURCE, encoding="utf-8")
            result = CSharpLexicalAnalyzer().analyze(scan_repository(root))
            index: dict[str, int] = {}
            for symbol in result.symbols:
                index.update(symbol.metadata.get("name_index", {}))
            self.assertIn("Creature", index)
            self.assertIn("Initialize", index)

    def test_coverage_reports_every_eligible_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "A.cs").write_text(SOURCE, encoding="utf-8")
            (root / "B.cs").write_text("public class B {}\n", encoding="utf-8")
            result = CSharpLexicalAnalyzer().analyze(scan_repository(root))
            self.assertEqual(result.coverage[0].eligible_files, 2)
            self.assertEqual(result.coverage[0].analyzed_files, 2)
            self.assertFalse(result.coverage[0].failures)


class CSharpDeclaredValueTests(TestCase):
    """C# recorded no declared values at all, in any of the three surfaces.

    A reader-parity check found it in one run, after four fixtures had each
    found the same gap one language at a time. C# is most of .NET and all of
    Unity scripting, so a repository written in it stated no constants and no
    vocabularies anywhere in its specification.
    """

    SOURCE = (
        "public static class Policy {\n"
        "    public const int MaxRetries = 10;\n"
        '    public const string ServiceName = "checkout";\n'
        "    public static readonly int Timeout = 30;\n"
        "    public const int Computed = MaxRetries * 2;\n"
        "}\n"
        "// const int Commented = 5;\n"
        "public enum Method { Get, Put, Head }\n"
        "public enum Ordered { A = 1, B = 2 }\n"
        "public enum One { Only }\n"
    )

    def _constants(self) -> dict[str, Any]:
        return declared_constants(self.SOURCE, blank_noise(self.SOURCE))

    def test_a_const_literal_is_recorded_with_its_value(self) -> None:
        self.assertEqual(self._constants()["MaxRetries"]["value"], "10")

    def test_a_string_constant_survives_the_blanking(self) -> None:
        # Blanking removes a string body and its quotes, so the value has to
        # come from the original text at the same offsets. Trimming the match
        # on the blanked text left the group holding a lone quote.
        self.assertEqual(self._constants()["ServiceName"]["value"], "checkout")

    def test_static_readonly_counts_as_a_declared_value(self) -> None:
        self.assertEqual(self._constants()["Timeout"]["value"], "30")

    def test_a_computed_value_is_not_recorded(self) -> None:
        self.assertNotIn("Computed", self._constants())

    def test_a_constant_in_a_comment_is_not_a_constant(self) -> None:
        self.assertNotIn("Commented", self._constants())

    def test_enum_members_are_a_vocabulary(self) -> None:
        found = declared_enums(blank_noise(self.SOURCE))
        self.assertEqual(found["Method"]["members"], ["Get", "Put", "Head"])

    def test_an_assigned_ordinal_does_not_become_a_member(self) -> None:
        found = declared_enums(blank_noise(self.SOURCE))
        self.assertEqual(found["Ordered"]["members"], ["A", "B"])

    def test_one_member_is_not_a_vocabulary(self) -> None:
        self.assertNotIn("One", declared_enums(blank_noise(self.SOURCE)))
