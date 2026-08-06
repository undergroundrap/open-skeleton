# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository
from open_skeleton.spec import build_spec, load_profile, render_spec_markdown, verify_spec
from open_skeleton.spec.panels import PanelContext, build_panel, short_form
from open_skeleton.spec.probes import LedgerCorpus, run_probe
from open_skeleton.spec.profile import ProfileError, SpecProbe, parse_profile
from open_skeleton.spec.render import (
    RenderedClaim,
    _spread_by_category,
    render_spec_index_json,
    render_spec_json,
)
from open_skeleton.spec.substitutes import derive_substitutes
from tests.helpers import create_sample_repository


def _analyzed(root: Path, state: Path) -> EvidenceLedger:
    snapshot = scan_repository(root)
    result = analyze_snapshot(snapshot)
    ledger = EvidenceLedger(state / "evidence.sqlite3")
    ledger.save_snapshot(snapshot)
    ledger.save_analysis(result)
    return ledger


class ProfileTests(TestCase):
    def test_standard_profile_loads_and_numbers_every_section(self) -> None:
        profile = load_profile()
        sections = profile.walk()
        self.assertGreaterEqual(len(sections), 30)
        self.assertTrue(all(section.number for section in sections))
        self.assertEqual(len({section.section_id for section in sections}), len(sections))

    def test_duplicate_section_ids_are_rejected(self) -> None:
        payload = {
            "schema": "open-skeleton.spec_profile.v1",
            "profile_id": "p",
            "profile_version": "v1",
            "title": "t",
            "sections": [
                {"id": "a", "number": "1", "title": "A"},
                {"id": "a", "number": "2", "title": "A again"},
            ],
        }
        with self.assertRaises(ProfileError):
            parse_profile(payload)

    def test_unknown_cross_reference_is_rejected(self) -> None:
        payload = {
            "schema": "open-skeleton.spec_profile.v1",
            "profile_id": "p",
            "profile_version": "v1",
            "title": "t",
            "sections": [
                {
                    "id": "a",
                    "number": "1",
                    "title": "A",
                    "cross_references": ["does-not-exist"],
                }
            ],
        }
        with self.assertRaises(ProfileError):
            parse_profile(payload)

    def test_unsupported_probe_kind_is_rejected(self) -> None:
        payload = {
            "schema": "open-skeleton.spec_profile.v1",
            "profile_id": "p",
            "profile_version": "v1",
            "title": "t",
            "sections": [
                {
                    "id": "a",
                    "number": "1",
                    "title": "A",
                    "probes": [{"name": "n", "kind": "grep_source", "terms": ["x"]}],
                }
            ],
        }
        with self.assertRaises(ProfileError):
            parse_profile(payload)


class ProbeTests(TestCase):
    def _corpus(self) -> LedgerCorpus:
        return LedgerCorpus(
            snapshot_id="snap",
            files=({"path": "backend/main.py", "language": "Python", "role": "source"},),
            claims=(
                {
                    "claim_id": "census",
                    "category": "delivery_automation",
                    "supporting_evidence": ["virtual"],
                },
                {
                    "claim_id": "real",
                    "category": "storage",
                    "supporting_evidence": ["sourced"],
                },
            ),
            symbols=(),
            edges=(),
            evidence=(
                {"evidence_id": "virtual", "path": "."},
                {"evidence_id": "sourced", "path": "backend/main.py"},
            ),
        )

    def test_sourced_probe_ignores_repository_wide_census_receipts(self) -> None:
        corpus = self._corpus()
        counted = run_probe(
            SpecProbe("Delivery", "claim_category", ("delivery_automation",)), corpus
        )
        sourced = run_probe(
            SpecProbe("Delivery", "sourced_claim_category", ("delivery_automation",)),
            corpus,
        )
        self.assertEqual(counted.match_count, 1)
        self.assertEqual(sourced.match_count, 0)

    def test_sourced_probe_counts_claims_backed_by_a_real_file(self) -> None:
        result = run_probe(
            SpecProbe("Storage", "sourced_claim_category", ("storage",)), self._corpus()
        )
        self.assertEqual(result.match_count, 1)

    def test_path_glob_matches_basename_and_full_path(self) -> None:
        corpus = self._corpus()
        self.assertEqual(
            run_probe(SpecProbe("Py", "path_glob", ("main.py",)), corpus).match_count, 1
        )
        self.assertEqual(
            run_probe(SpecProbe("Py", "path_glob", ("backend/*.py",)), corpus).match_count,
            1,
        )
        self.assertEqual(
            run_probe(SpecProbe("Docker", "path_glob", ("Dockerfile",)), corpus).match_count,
            0,
        )


class SpecDocumentTests(TestCase):
    def test_absent_concern_records_the_query_that_found_nothing(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            document = build_spec(ledger, load_profile())
            containerization = next(
                item
                for item in document.sections
                if item.section_id == "operations.containerization"
            )
            self.assertEqual(containerization.verdict, "absent")
            self.assertTrue(containerization.probe_results)
            self.assertTrue(all(item.match_count == 0 for item in containerization.probe_results))

            markdown = render_spec_markdown(document)
            self.assertIn("Determination: absent", markdown)
            self.assertIn("path_glob: Dockerfile", markdown)

    def test_every_claim_reaches_exactly_one_section(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            document = build_spec(ledger, load_profile())
            rendered = [
                claim.claim_id for section in document.sections for claim in section.findings
            ]
            self.assertEqual(len(rendered), len(set(rendered)))
            self.assertEqual(len(rendered), document.total_claims)

    def test_json_projection_is_stable_and_carries_receipts(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            document = build_spec(ledger, load_profile())
            payload = json.loads(render_spec_json(document))
            self.assertEqual(payload["schema"], "open-skeleton.spec.v1")

            # Generation time is the only field allowed to move between runs.
            repeated = json.loads(render_spec_json(build_spec(ledger, load_profile())))
            payload.pop("generated_at")
            repeated.pop("generated_at")
            self.assertEqual(payload, repeated)
            cited = [
                citation
                for section in payload["sections"]
                for claim in section["findings"]
                for citation in claim["citations"]
            ]
            self.assertTrue(cited)
            self.assertTrue(all(item["evidence_id"] for item in cited))

    def test_the_json_projection_carries_every_symbol_the_ledger_holds(self) -> None:
        """The readable index is a selection; the JSON must not be one.

        A consumer that has to re-parse the repository to find a name the
        analyzers already extracted is being handed a summary, not data.
        """

        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            document = build_spec(ledger, load_profile())
            payload = json.loads(render_spec_json(document))
            snapshot = ledger.latest_snapshot()
            assert snapshot is not None
            expected = {
                str(item["qualified_name"])
                for item in ledger.list_symbols(snapshot["snapshot_id"], limit=5_000)
            }
            index = json.loads(render_spec_index_json(document))

            self.assertTrue(expected)
            self.assertEqual({item["qualified_name"] for item in index["symbols"]}, expected)
            # The document itself must stay free of both inventories: they
            # scale with the repository and a reader of one section should not
            # have to parse them.
            self.assertNotIn("symbols", payload)
            self.assertNotIn("name_index", payload)
            for item in index["symbols"]:
                self.assertTrue(item["path"])
                self.assertTrue(item["kind"])
                self.assertTrue(item["short_form"])

    def test_the_short_form_drops_the_package_path_and_enclosing_class(self) -> None:
        # Imports, stack traces and review comments all use this spelling, so a
        # reader searching for the name they know has to be able to find it.
        self.assertEqual(
            short_form("backend.app.core.scaling_math.ScalingMath.get_xp_required"),
            "scaling_math.get_xp_required",
        )
        self.assertEqual(short_form("backend.app.core.dungeon_engine.roll"), "dungeon_engine.roll")
        self.assertEqual(short_form("solitary"), "solitary")

    def test_diagrams_are_omitted_with_a_reason_rather_than_invented(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            document = build_spec(ledger, load_profile())
            routes = next(item for item in document.sections if item.section_id == "surface.http")
            diagram = routes.diagrams[0]
            self.assertIsNone(diagram.mermaid)
            self.assertIsNotNone(diagram.omitted_reason)


class CitationIntegrityTests(TestCase):
    def test_untouched_repository_verifies_at_full_integrity(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            document = build_spec(ledger, load_profile())
            report = verify_spec(document, ledger, root=root)
            self.assertGreater(report.total, 0)
            self.assertEqual(report.failures, ())
            self.assertEqual(report.integrity, 1.0)

    def test_editing_a_cited_file_is_reported_rather_than_hidden(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            document = build_spec(ledger, load_profile())
            cited_path = next(
                citation.path
                for section in document.sections
                for claim in section.findings
                for citation in claim.citations
                if citation.file_sha256 is not None
            )
            target = root / cited_path
            target.write_text(
                target.read_text(encoding="utf-8") + "\n# edited after the snapshot\n",
                encoding="utf-8",
            )
            report = verify_spec(document, ledger, root=root)

            self.assertLess(report.integrity, 1.0)
            self.assertTrue(any(item.status == "source-changed" for item in report.failures))


class DependencyProbeTests(TestCase):
    def _corpus(self) -> LedgerCorpus:
        return LedgerCorpus(
            snapshot_id="snap",
            files=(),
            claims=(),
            symbols=(),
            edges=(
                {
                    "relationship": "declares_dependency",
                    "source_path": "pyproject.toml",
                    "target_ref": "pip-audit",
                },
                {
                    "relationship": "declares_dependency",
                    "source_path": "package.json",
                    "target_ref": "@opentelemetry/api",
                },
                {
                    "relationship": "imports",
                    "source_path": "app.py",
                    "target_ref": "opentelemetry.trace",
                },
            ),
        )

    def test_dependency_name_matches_scoped_packages(self) -> None:
        result = run_probe(
            SpecProbe("Telemetry", "dependency_name", ("@opentelemetry/*",)),
            self._corpus(),
        )
        self.assertEqual(result.match_count, 1)

    def test_dependency_name_does_not_match_imports(self) -> None:
        result = run_probe(
            SpecProbe("Telemetry", "dependency_name", ("opentelemetry*",)),
            self._corpus(),
        )
        self.assertEqual(result.matches, ("@opentelemetry/api",))

    def test_import_target_matches_dotted_module_head(self) -> None:
        result = run_probe(
            SpecProbe("Telemetry", "import_target", ("opentelemetry*",)), self._corpus()
        )
        self.assertEqual(result.matches, ("opentelemetry.trace",))

    def test_probes_do_not_match_unrelated_names(self) -> None:
        result = run_probe(
            SpecProbe("Cloud", "dependency_name", ("boto3", "azure-*")), self._corpus()
        )
        self.assertEqual(result.match_count, 0)


class PanelTests(TestCase):
    FILES = (
        {
            "path": "a.py",
            "language": "Python",
            "role": "source",
            "line_count": 100,
            "size_bytes": 1000,
            "sha256": "a" * 64,
        },
        {
            "path": "b.py",
            "language": "Python",
            "role": "source",
            "line_count": 50,
            "size_bytes": 500,
            "sha256": "b" * 64,
        },
        {
            "path": "c.md",
            "language": "Markdown",
            "role": "documentation",
            "line_count": 10,
            "size_bytes": 100,
            "sha256": "c" * 64,
        },
    )

    def test_language_census_reports_shares_that_sum_sensibly(self) -> None:
        panel = build_panel("language_census", PanelContext(files=self.FILES))
        self.assertEqual(panel.rows[0][0], "Python")
        self.assertEqual(panel.rows[0][1], "2")
        self.assertEqual(panel.rows[0][3], "150")

    def test_largest_files_orders_by_line_count(self) -> None:
        panel = build_panel("largest_files", PanelContext(files=self.FILES))
        self.assertEqual([row[0] for row in panel.rows], ["a.py", "b.py", "c.md"])

    def test_exclusions_panel_states_that_content_was_never_read(self) -> None:
        panel = build_panel(
            "exclusions",
            PanelContext(
                files=self.FILES,
                exclusions=(
                    {"path": "x", "reason": "binary"},
                    {"path": "y", "reason": "binary"},
                ),
            ),
        )
        self.assertEqual(panel.rows, (("binary", "2"),))
        assert panel.note is not None
        self.assertIn("never read", panel.note)

    def test_composition_section_renders_panels_in_the_document(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            document = build_spec(ledger, load_profile())
            composition = next(
                item for item in document.sections if item.section_id == "introduction.composition"
            )
            self.assertEqual(len(composition.panels), 5)

            markdown = render_spec_markdown(document)
            self.assertIn("Composition by language", markdown)
            self.assertIn("Excluded entries by reason", markdown)


class ClaimYieldTests(TestCase):
    def test_yield_is_reported_beside_coverage(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            coverage = ledger.analysis_coverage(
                str(ledger.latest_snapshot()["snapshot_id"])  # type: ignore[index]
            )
            self.assertTrue(coverage)
            for record in coverage:
                self.assertIn("yield_ratio", record)
                self.assertLessEqual(record["claimed_files"], record["analyzed_files"])
                # None means "not recorded" — an analyzer with nothing eligible, or
                # a row written before the column existed. It is not a yield of zero.
                if record["yield_ratio"] is not None:
                    self.assertGreaterEqual(record["yield_ratio"], 0.0)
                    self.assertLessEqual(record["yield_ratio"], 1.0)

    def test_spec_coverage_table_distinguishes_reach_from_findings(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            markdown = render_spec_markdown(build_spec(ledger, load_profile()))
            self.assertIn("| Coverage | Yield |", markdown)
            self.assertIn("yield column is what distinguishes", markdown.replace("\n", " "))


class AppendixPanelTests(TestCase):
    SYMBOLS = (
        {
            "path": "backend/main.py",
            "kind": "module",
            "qualified_name": "backend.main",
            "metadata": {
                "tunables": {
                    "BAG_SIZE": {"value": 16.0, "line": 21},
                    "COOLDOWN": {"value": 1.5, "line": 22},
                }
            },
        },
        {
            "path": "backend/main.py",
            "kind": "async_function",
            "qualified_name": "backend.main.equip",
            "metadata": {
                "routes": [{"method": "POST", "path": "/action/equip"}],
                "control_flow": [
                    {"kind": "guard", "line": 100, "label": "not item", "depth": 0},
                    {"kind": "raise", "line": 101, "label": "HTTP 400", "depth": 1},
                    {"kind": "return", "line": 110, "label": "ok", "depth": 0},
                ],
            },
        },
        {
            "path": "backend/util.py",
            "kind": "function",
            "qualified_name": "backend.util.helper",
            "metadata": {
                "control_flow": [{"kind": "raise", "line": 5, "label": "HTTP 500", "depth": 0}]
            },
        },
    )

    def test_tunables_render_integers_without_a_trailing_zero(self) -> None:
        panel = build_panel("tunable_index", PanelContext(symbols=self.SYMBOLS))
        values = {row[0]: row[1] for row in panel.rows}
        self.assertEqual(values["BAG_SIZE"], "16")
        self.assertEqual(values["COOLDOWN"], "1.5")

    def test_every_tunable_carries_a_definition_site(self) -> None:
        panel = build_panel("tunable_index", PanelContext(symbols=self.SYMBOLS))
        for row in panel.rows:
            self.assertRegex(row[2], r"\.py:\d+$")

    def test_the_failure_surface_lists_raises_reachable_from_a_route(self) -> None:
        panel = build_panel("failure_surface", PanelContext(symbols=self.SYMBOLS))
        raised = {row[0] for row in panel.rows}
        self.assertEqual(raised, {"HTTP 400"})

    def test_a_raise_outside_a_route_handler_is_not_a_response(self) -> None:
        # A helper can raise without that failure being reachable as a response.
        panel = build_panel("failure_surface", PanelContext(symbols=self.SYMBOLS))
        self.assertNotIn("HTTP 500", {row[0] for row in panel.rows})

    def test_both_panels_state_what_they_exclude(self) -> None:
        for name in ("tunable_index", "failure_surface"):
            panel = build_panel(name, PanelContext(symbols=self.SYMBOLS))
            assert panel.note is not None
            self.assertIn("not", panel.note.lower())


class DocumentedValueTests(TestCase):
    """Documentation is the artifact that goes stale in silence."""

    def _panel(self, tunables: dict[str, Any], documented: dict[str, Any]) -> Any:
        symbols = (
            {"path": "app.py", "metadata": {"tunables": tunables}},
            {"path": "README.md", "metadata": {"documented_facts": documented}},
        )
        return build_panel("documented_values", PanelContext(symbols=symbols))

    def test_a_documented_value_that_differs_from_the_constant_is_flagged(self) -> None:
        panel = self._panel(
            {"BAG_SIZE": {"value": 16.0, "line": 4}},
            {"BAG_SIZE": {"line": 9, "values": ["20"]}},
        )
        self.assertEqual(panel.rows[0][1], "`20`")
        self.assertEqual(panel.rows[0][2], "`16`")
        self.assertIn("disagrees", panel.rows[0][3])

    def test_a_matching_value_agrees(self) -> None:
        panel = self._panel(
            {"BAG_SIZE": {"value": 16.0, "line": 4}},
            {"BAG_SIZE": {"line": 9, "values": ["16"]}},
        )
        self.assertEqual(panel.rows[0][3], "agrees")

    def test_a_name_the_code_does_not_declare_is_left_unjudged(self) -> None:
        # Calling a function name wrong because it has no constant of that
        # name would be a fabricated finding.
        panel = self._panel({}, {"run_dungeon": {"line": 3, "values": []}})
        self.assertEqual(panel.rows[0][3], "not a declared constant")

    def test_the_note_says_how_many_could_be_checked_at_all(self) -> None:
        # Reporting zero disagreements without saying zero were comparable
        # reads as a clean bill of health that was never earned.
        panel = self._panel({}, {"run_dungeon": {"line": 3, "values": []}})
        assert panel.note is not None
        self.assertIn("0 of them", panel.note)


class NameIndexTests(TestCase):
    """A concordance: every name a file mentions, for navigation not judgement."""

    def test_the_json_projection_carries_a_name_index_the_markdown_does_not(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            document = build_spec(ledger, load_profile())
            payload = json.loads(render_spec_index_json(document))
            index = payload["name_index"]

            self.assertTrue(index, "expected at least one file to contribute names")
            for path, names in index.items():
                self.assertIsInstance(path, str)
                self.assertTrue(names)
                for name, line in names.items():
                    self.assertIsInstance(name, str)
                    self.assertGreaterEqual(int(line), 1)

    def test_a_local_binding_reaches_the_index_but_not_the_readable_symbol_table(self) -> None:
        # The concordance is exhaustive on purpose; the readable index is not.
        # Presenting a loop variable as the equal of a public function would
        # bury the surface that matters under the noise that does not.
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            (root / "app.py").write_text(
                "def handler():\n    scratch_local_value = 3\n    return scratch_local_value\n",
                encoding="utf-8",
            )
            ledger = _analyzed(root, workspace / "state")

            document = build_spec(ledger, load_profile())
            payload = json.loads(render_spec_index_json(document))
            every_name = {name for names in payload["name_index"].values() for name in names}
            self.assertIn("scratch_local_value", every_name)
            self.assertNotIn(
                "scratch_local_value",
                {item["qualified_name"] for item in payload["symbols"]},
            )


class SubstituteTests(TestCase):
    """An absent verdict that stops at the absence sends a reader to grep."""

    def test_a_static_lookup_table_is_not_a_place_work_waits(self) -> None:
        # Being initialised to a mutable literal is not enough: a dict of room
        # names never changes, and calling it a queue fabricates a property.
        symbols = (
            {
                "path": "e.py",
                "qualified_name": "e._ROOM_NAMES",
                "start_line": 3,
                "metadata": {"mutable_initializer": True},
            },
        )
        self.assertEqual(
            derive_substitutes(symbols, (), absent_sections=frozenset({"integration.messaging"})),
            (),
        )

    def test_a_container_written_to_at_runtime_is_reported(self) -> None:
        symbols = (
            {"path": "m.py", "qualified_name": "m._pending", "start_line": 7, "metadata": {}},
        )
        claims = (
            {
                "category": "process_local_state",
                "claim": "m._pending is a module-owned mutable container with observed mutation sites.",
            },
        )
        result = derive_substitutes(
            symbols, claims, absent_sections=frozenset({"integration.messaging"})
        )
        self.assertEqual(result[0].structures[0].name, "m._pending")
        self.assertEqual(result[0].structures[0].location, "m.py:7")

    def test_a_constant_compared_in_a_guard_is_a_threshold(self) -> None:
        symbols = (
            {
                "path": "a.py",
                "qualified_name": "a",
                "start_line": 1,
                "metadata": {"tunables": {"BAG_SIZE": {"value": 16.0, "line": 4}}},
            },
            {
                "path": "a.py",
                "qualified_name": "a.put",
                "start_line": 9,
                "metadata": {
                    "control_flow": [{"kind": "guard", "label": "len(bag) >= BAG_SIZE", "line": 11}]
                },
            },
        )
        result = derive_substitutes(
            symbols, (), absent_sections=frozenset({"operations.observability"})
        )
        self.assertIn("BAG_SIZE = 16", result[0].structures[0].name)
        self.assertEqual(result[0].structures[0].location, "a.py:11")

    def test_a_constant_never_compared_is_not_a_threshold(self) -> None:
        symbols = (
            {
                "path": "a.py",
                "qualified_name": "a",
                "start_line": 1,
                "metadata": {"tunables": {"BAG_SIZE": {"value": 16.0, "line": 4}}},
            },
            {
                "path": "a.py",
                "qualified_name": "a.put",
                "start_line": 9,
                "metadata": {"control_flow": [{"kind": "guard", "label": "ready", "line": 11}]},
            },
        )
        self.assertEqual(
            derive_substitutes(
                symbols, (), absent_sections=frozenset({"operations.observability"})
            ),
            (),
        )

    def test_a_rule_does_not_fire_when_the_concern_is_present(self) -> None:
        # Firing on an absent concern with nothing to show would assert a
        # substitute exists on the strength of the concern being missing.
        symbols = (
            {"path": "m.py", "qualified_name": "m._pending", "start_line": 7, "metadata": {}},
        )
        claims = (
            {
                "category": "process_local_state",
                "claim": "m._pending is a module-owned mutable container.",
            },
        )
        self.assertEqual(derive_substitutes(symbols, claims, absent_sections=frozenset()), ())


class ExaminedFileTests(TestCase):
    """The reverse of a citation: which files a section was written from."""

    def test_a_section_lists_the_files_its_findings_were_read_from(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            document = build_spec(ledger, load_profile())
            with_findings = [item for item in document.sections if item.findings]
            self.assertTrue(with_findings)
            for section in with_findings:
                # Constraints are cited exactly as findings are, so a file
                # reached only through one still had to be read.
                cited = {
                    citation.path
                    for claim in (*section.findings, *section.constraints)
                    for citation in claim.citations
                    if citation.path not in {".", ""}
                }
                listed = {path for path, _ in section.examined_files}
                self.assertEqual(listed, cited)

    def test_a_repository_wide_census_receipt_names_no_file(self) -> None:
        # A section resting only on census receipts has examined nothing in
        # particular, and claiming the whole tree would be worse than silence.
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            document = build_spec(ledger, load_profile())
            for section in document.sections:
                for path, count in section.examined_files:
                    self.assertNotEqual(path, ".")
                    self.assertGreaterEqual(count, 1)

    def test_counts_are_ordered_by_how_much_each_file_contributed(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")

            document = build_spec(ledger, load_profile())
            for section in document.sections:
                counts = [count for _, count in section.examined_files]
                self.assertEqual(counts, sorted(counts, reverse=True))


class SecurityMatrixTests(TestCase):
    """One table a reviewer can act on, restating verdicts already reached."""

    def _matrix(self, verdicts: dict[str, str], claims: dict[str, Any]) -> Any:
        return build_panel(
            "security_matrix",
            PanelContext(section_verdicts=verdicts, claims_by_category=claims),
        )

    def test_a_present_section_is_reported_present(self) -> None:
        panel = self._matrix(
            {"security.boundaries": "applicable"},
            {"security_boundary": ({"claim_id": "c1"},)},
        )
        row = next(item for item in panel.rows if item[4] == "security.boundaries")
        self.assertEqual(row[1], "present")
        self.assertEqual(row[2], "1")

    def test_an_absent_control_does_not_count_its_absence_claims_as_evidence(self) -> None:
        # An authentication census reporting that no route declares one is not
        # a point in authentication's favour. Counting it would repeat the
        # inversion the sourced-claim probe exists to prevent.
        panel = self._matrix(
            {"security.authentication": "absent"},
            {"auth_control_census": ({"claim_id": "c1"},)},
        )
        row = next(item for item in panel.rows if item[4] == "security.authentication")
        self.assertEqual(row[1], "**absent**")
        self.assertEqual(row[2], "—")

    def test_a_section_the_profile_never_probes_is_distinguished_from_absent(self) -> None:
        panel = self._matrix({}, {})
        states = {item[1] for item in panel.rows}
        self.assertEqual(states, {"not probed"})

    def test_a_degenerate_verdict_reads_as_partial(self) -> None:
        panel = self._matrix({"security.data-protection": "degenerate"}, {})
        row = next(item for item in panel.rows if item[4] == "security.data-protection")
        self.assertEqual(row[1], "partial")

    def test_every_row_names_the_section_that_evidences_it(self) -> None:
        panel = self._matrix({}, {})
        for row in panel.rows:
            self.assertTrue(row[4])


class EndpointCatalogTests(TestCase):
    """A route census says a path exists; this says what the handler does."""

    def _catalog(self, symbols: tuple[dict[str, Any], ...]) -> Any:
        return build_panel("endpoint_catalog", PanelContext(symbols=symbols))

    def _handler(self, **metadata: Any) -> dict[str, Any]:
        base = {"routes": [{"method": "POST", "path": "/x", "start_line": 9}]}
        base.update(metadata)
        return {"path": "main.py", "qualified_name": "app.main.act", "metadata": base}

    def test_a_route_reports_its_handler_and_declaration_site(self) -> None:
        panel = self._catalog((self._handler(),))
        self.assertEqual(panel.rows[0][0], "POST")
        self.assertEqual(panel.rows[0][2], "main.act")
        self.assertEqual(panel.rows[0][6], "main.py:9")

    def test_guards_are_counted_and_http_refusals_are_named(self) -> None:
        panel = self._catalog(
            (
                self._handler(
                    control_flow=[
                        {"kind": "guard", "label": "not player", "line": 10},
                        {"kind": "raise", "label": "HTTP 404", "line": 11},
                        {"kind": "raise", "label": "ValueError", "line": 12},
                    ]
                ),
            )
        )
        self.assertEqual(panel.rows[0][3], "1")
        # A non-HTTP raise is a failure but not a refusal a caller sees as a status.
        self.assertEqual(panel.rows[0][4], "HTTP 404")

    def test_response_fields_join_by_function_name_not_symbol(self) -> None:
        # Payload shapes are recorded against the declaring module, so a join
        # by symbol identity finds nothing and every row reads as fieldless.
        module = {
            "path": "main.py",
            "qualified_name": "app.main",
            "metadata": {"payload_shapes": {"act": {"fields": ["ok", "message"], "line": 9}}},
        }
        panel = self._catalog((self._handler(), module))
        self.assertIn("`ok`", panel.rows[0][5])
        self.assertIn("`message`", panel.rows[0][5])

    def test_a_handler_with_no_guard_is_reported_as_such(self) -> None:
        panel = self._catalog((self._handler(),))
        self.assertEqual(panel.rows[0][3], "0")
        assert panel.note is not None
        self.assertIn("no guard recorded", panel.note)

    def test_a_symbol_without_routes_contributes_no_row(self) -> None:
        self.assertEqual(self._catalog(({"path": "x.py", "metadata": {}},)).rows, ())


class DataFlowTests(TestCase):
    """Where data enters, rests, and leaves — at a granularity that is knowable."""

    def _flow(
        self, symbols: tuple[Any, ...], claims: dict[str, Any], locations: dict[str, str]
    ) -> Any:
        return build_panel(
            "data_flow",
            PanelContext(symbols=symbols, claims_by_category=claims, claim_locations=locations),
        )

    def test_a_module_serving_routes_is_an_entry_point(self) -> None:
        panel = self._flow(
            (
                {
                    "path": "main.py",
                    "qualified_name": "app.main.act",
                    "metadata": {"routes": [{"method": "GET", "path": "/x"}]},
                },
            ),
            {},
            {},
        )
        self.assertEqual(panel.rows[0][0], "app.main")
        self.assertEqual(panel.rows[0][1], "1")

    def test_a_module_naming_a_third_party_host_is_an_egress_point(self) -> None:
        panel = self._flow(
            (
                {
                    "path": "layout.tsx",
                    "qualified_name": "app.layout",
                    "metadata": {"external_origins": {"cdn.example.com": {"count": 1}}},
                },
            ),
            {},
            {},
        )
        self.assertEqual(panel.rows[0][4], "cdn.example.com")

    def test_durable_tables_are_attributed_to_the_owner_in_the_claim(self) -> None:
        panel = self._flow(
            (),
            {
                "storage_schema": (
                    {"claim_id": "c1", "claim": "db.DBManager._init creates SQLite table players."},
                )
            },
            {"c1": "db.py:12"},
        )
        self.assertEqual(panel.rows[0][0], "db.DBManager")
        self.assertEqual(panel.rows[0][2], "players")

    def test_a_module_with_nothing_to_report_is_omitted(self) -> None:
        # A row of dashes is noise; the file census already lists every module.
        panel = self._flow(
            (
                {
                    "path": "x.py",
                    "qualified_name": "app.x",
                    "metadata": {"imported_names": {"os": {}}},
                },
            ),
            {},
            {},
        )
        self.assertEqual(panel.rows, ())

    def test_the_note_refuses_to_claim_a_traced_path(self) -> None:
        panel = self._flow((), {}, {})
        assert panel.note is not None
        self.assertIn("not one where a path", panel.note)


class PreconditionTests(TestCase):
    """A concern that presupposes another is not a gap when the other is absent."""

    def _profile(self, requires: list[str], order: str = "before") -> Any:
        first = {
            "id": "a.surface",
            "number": "1",
            "title": "Surface",
            "probes": [{"name": "p", "kind": "path_glob", "terms": ["never-matches-*"]}],
        }
        second = {
            "id": "a.derived",
            "number": "2",
            "title": "Derived",
            "probes": [{"name": "q", "kind": "path_glob", "terms": ["also-never-*"]}],
            "requires": requires,
        }
        pair = [first, second] if order == "before" else [second, first]
        return parse_profile(
            {
                "schema": "open-skeleton.spec_profile.v1",
                "profile_id": "p",
                "profile_version": "v1",
                "title": "t",
                "sections": pair,
            }
        )

    def _verdicts(self, profile: Any) -> dict[str, str]:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            create_sample_repository(root)
            ledger = _analyzed(root, workspace / "state")
            document = build_spec(ledger, profile)
        return {item.section_id: item.verdict for item in document.sections}

    def test_an_unmet_precondition_turns_absent_into_not_applicable(self) -> None:
        verdicts = self._verdicts(self._profile(["a.surface"]))
        self.assertEqual(verdicts["a.surface"], "absent")
        self.assertEqual(verdicts["a.derived"], "not_applicable")

    def test_without_a_precondition_the_same_section_is_a_gap(self) -> None:
        verdicts = self._verdicts(self._profile([]))
        self.assertEqual(verdicts["a.derived"], "absent")

    def test_a_forward_requirement_is_rejected_rather_than_silently_wrong(self) -> None:
        # Requirements are read in one ordered pass, so a section depending on
        # a later one would be judged against a verdict not yet decided.
        with self.assertRaises(ProfileError):
            self._profile(["a.surface"], order="after")

    def test_an_unknown_requirement_is_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            self._profile(["does.not.exist"])

    def test_the_standard_profile_ties_each_precondition_to_its_own_verdict(self) -> None:
        # The relationship is the invariant, not any one verdict: a concern
        # that presupposes an HTTP surface may only be excused when that
        # surface is itself missing. Asserting a fixed verdict here would pass
        # or fail on what the sample repository happens to contain.
        verdicts = self._verdicts(load_profile())
        profile = load_profile()
        by_id = {section.section_id: section for section in profile.walk()}
        checked = 0
        for section_id, verdict in verdicts.items():
            if verdict != "not_applicable":
                continue
            requires = by_id[section_id].requires
            self.assertTrue(requires, f"{section_id} is not applicable but requires nothing")
            self.assertTrue(
                any(verdicts.get(name) not in {"applicable", "degenerate"} for name in requires),
                f"{section_id} was excused while every precondition it names was met",
            )
            checked += 1
        self.assertTrue(checked, "expected the sample repository to excuse at least one concern")


class SummarySpreadTests(TestCase):
    """The summary shows the most important findings, not the most numerous.

    Found by reading the specification this tool writes about its own
    repository. Six of ten headline rows said a file was long, while the
    storage schema, the migration behaviour and the entry points -- all equally
    high importance -- never appeared. Taking the first N by rank lets whichever
    high-importance category happens to be largest fill the summary.
    """

    def _claim(self, category: str, name: str) -> RenderedClaim:
        return RenderedClaim(
            claim_id=name,
            claim=name,
            category=category,
            status="verified",
            confidence=1.0,
            importance="high",
            citations=(),
        )

    def test_one_category_cannot_fill_the_summary(self) -> None:
        crowded = [self._claim("concentration", f"long-{index}") for index in range(20)]
        crowded += [self._claim("storage_schema", "table"), self._claim("entry", "main")]
        chosen = _spread_by_category(crowded, 10)
        self.assertEqual(len(chosen), 10)
        self.assertIn("storage_schema", {item.category for item in chosen})
        self.assertIn("entry", {item.category for item in chosen})

    def test_ranking_within_a_category_is_preserved(self) -> None:
        # Presentation order changes; which claim outranks which does not.
        ordered = [self._claim("a", f"a{index}") for index in range(4)]
        chosen = [item.claim for item in _spread_by_category(ordered, 4)]
        self.assertEqual(chosen, ["a0", "a1", "a2", "a3"])

    def test_a_short_list_is_returned_whole(self) -> None:
        few = [self._claim("a", "one"), self._claim("b", "two")]
        self.assertEqual(len(_spread_by_category(few, 10)), 2)

    def test_an_empty_list_yields_nothing(self) -> None:
        self.assertEqual(_spread_by_category([], 10), [])
