# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.project_metadata import (
    ProjectMetadataAnalyzer,
    _cargo_dependencies,
    _cargo_name,
    _checked_out_revision,
    _declared_commands,
    _declared_commitments,
    _declared_license,
    is_declarative_document,
    is_non_goal_heading,
)
from open_skeleton.models import AnalysisResult
from open_skeleton.scanner import scan_repository


class ProjectMetadataAnalyzerTests(TestCase):
    def test_reports_documented_but_absent_tailwind_as_conflict(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(
                "| Frontend | Tailwind CSS + globals.css |\n", encoding="utf-8"
            )
            (root / "package.json").write_text(
                '{"dependencies":{"react":"latest"},"devDependencies":{}}',
                encoding="utf-8",
            )

            result = ProjectMetadataAnalyzer().analyze(scan_repository(root))
            drift = next(item for item in result.claims if item.category == "documentation_drift")

            self.assertEqual(drift.status, "conflict")
            self.assertTrue(drift.supporting_evidence)
            self.assertTrue(drift.contradicting_evidence)
            self.assertTrue(any(edge.target_ref == "react" for edge in result.edges))

    def test_tailwind_dependency_prevents_false_conflict(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("Uses Tailwind CSS.\n", encoding="utf-8")
            (root / "package.json").write_text(
                '{"devDependencies":{"tailwindcss":"latest"}}', encoding="utf-8"
            )

            result = ProjectMetadataAnalyzer().analyze(scan_repository(root))

            self.assertFalse(any(item.category == "documentation_drift" for item in result.claims))

    def test_reports_missing_package_test_script_and_ci_workflow(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(
                '{"scripts":{"build":"next build"}}', encoding="utf-8"
            )

            result = ProjectMetadataAnalyzer().analyze(scan_repository(root))
            categories = {item.category for item in result.claims}

            self.assertIn("testing_gap", categories)
            self.assertIn("delivery_automation", categories)

    def test_ci_workflow_prevents_absence_claim(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / ".github" / "workflows"
            workflow.mkdir(parents=True)
            (workflow / "ci.yml").write_text("name: CI\n", encoding="utf-8")

            result = ProjectMetadataAnalyzer().analyze(scan_repository(root))

            self.assertFalse(any(item.category == "delivery_automation" for item in result.claims))

    def test_extracts_requirements_documented_routes_and_documented_runtime_boundary(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text(
                "fastapi>=0.100\nopenai==1.0\n", encoding="utf-8"
            )
            (root / "README.md").write_text(
                """\
| Method | Path |
|---|---|
| `GET` | `/health` |
uvicorn main:app --port 8000
npm run dev
The app runs fully without LM Studio.
""",
                encoding="utf-8",
            )

            result = ProjectMetadataAnalyzer().analyze(scan_repository(root))
            categories = {item.category for item in result.claims}

            self.assertIn("documented_http_route_inventory", categories)
            self.assertIn("runtime_topology", categories)
            declared = {
                edge.target_ref
                for edge in result.edges
                if edge.relationship == "declares_dependency"
            }
            self.assertEqual(declared, {"fastapi", "openai"})

    def test_pipeline_compares_documented_routes_and_operator_dependencies_to_source(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            (root / "backend").mkdir()
            (root / "backend" / "main.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n"
                "@app.get('/health')\ndef health():\n    return {}\n"
                "@app.get('/ready')\ndef ready():\n    return {}\n",
                encoding="utf-8",
            )
            (root / "scripts" / "smoke.py").write_text("import requests\n", encoding="utf-8")
            (root / "backend" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
            (root / "README.md").write_text(
                "| `GET` | `/health` | documented |\n", encoding="utf-8"
            )

            result = analyze_snapshot(scan_repository(root))
            categories = {item.category for item in result.claims}

            self.assertIn("api_documentation_drift", categories)
            self.assertIn("dependency_drift", categories)


class PyprojectManifestTests(TestCase):
    MANIFEST = """\
[project]
name = "sample-project"
dependencies = ["httpx>=0.27", "pydantic[email]==2.*"]

[project.optional-dependencies]
dev = ["pip-audit>=2.8", "ruff"]
mcp = ["mcp>=2,<3"]
"""

    def _analyze(self, manifest: str) -> AnalysisResult:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(manifest, encoding="utf-8")
            snapshot = scan_repository(root)
            return ProjectMetadataAnalyzer().analyze(snapshot)

    def test_pep621_runtime_and_optional_dependencies_become_edges(self) -> None:
        result = self._analyze(self.MANIFEST)
        declared = {
            edge.target_ref for edge in result.edges if edge.relationship == "declares_dependency"
        }
        self.assertEqual(declared, {"httpx", "pydantic", "pip-audit", "ruff", "mcp"})

    def test_inventory_claim_separates_runtime_from_optional(self) -> None:
        result = self._analyze(self.MANIFEST)
        claim = next(item for item in result.claims if item.category == "dependency_inventory")
        self.assertIn("2 runtime", claim.claim)
        self.assertIn("3 optional", claim.claim)
        self.assertEqual(claim.status, "verified")

    def test_malformed_manifest_is_recorded_as_a_failure_not_a_guess(self) -> None:
        result = self._analyze("[project\nname = broken")
        self.assertEqual(
            [edge for edge in result.edges if edge.relationship == "declares_dependency"],
            [],
        )
        self.assertTrue(any(item.failures for item in result.coverage))

    def test_unsupported_tool_table_reports_zero_rather_than_partial(self) -> None:
        result = self._analyze('[tool.poetry.dependencies]\nrequests = "^2.0"\n')
        declared = [edge for edge in result.edges if edge.relationship == "declares_dependency"]
        self.assertEqual(declared, [])


class DeclaredCommandTests(TestCase):
    """The name a user types is declared in the manifest, not in the code.

    A `__main__` guard says a module can be run. It does not say the package
    installs a command called `open-skeleton`, and the engine reported the
    first and never the second -- so a document listed five runnable files and
    neither of the two commands the project actually ships.
    """

    def _commands(self, document: Mapping[str, Any], manifest: str) -> list[tuple[str, str]]:
        # `Mapping` rather than `dict`: the latter is invariant, so every
        # literal below would need its value type spelled out to match.
        return _declared_commands(dict(document), manifest)

    def test_python_console_scripts_are_read(self) -> None:
        document = {"project": {"scripts": {"tool": "pkg.cli:main"}}}
        self.assertEqual(self._commands(document, "pyproject.toml"), [("tool", "pkg.cli:main")])

    def test_python_gui_scripts_are_read_too(self) -> None:
        document = {"project": {"gui-scripts": {"tool-gui": "pkg.gui:main"}}}
        self.assertEqual(self._commands(document, "pyproject.toml"), [("tool-gui", "pkg.gui:main")])

    def test_a_node_bin_map_is_read(self) -> None:
        # A rule that understood only Python would report a Node package as
        # shipping no commands, which is a statement about this reader.
        document = {"name": "thing", "bin": {"thing": "./cli.js", "thing-dev": "./dev.js"}}
        self.assertEqual(
            self._commands(document, "package.json"),
            [("thing", "./cli.js"), ("thing-dev", "./dev.js")],
        )

    def test_a_node_bin_string_takes_the_package_name(self) -> None:
        # `"bin": "./cli.js"` installs one command named after the package.
        document = {"name": "thing", "bin": "./cli.js"}
        self.assertEqual(self._commands(document, "package.json"), [("thing", "./cli.js")])

    def test_a_cargo_binary_table_is_read(self) -> None:
        document = {"bin": [{"name": "hum", "path": "src/main.rs"}]}
        self.assertEqual(self._commands(document, "Cargo.toml"), [("hum", "src/main.rs")])

    def test_a_manifest_declaring_none_reports_none(self) -> None:
        self.assertEqual(self._commands({"project": {"name": "x"}}, "pyproject.toml"), [])

    def test_a_malformed_entry_is_skipped_rather_than_coerced(self) -> None:
        # A non-string target is not a command name; guessing at one would put
        # an invented entry point in the document.
        document = {"project": {"scripts": {"ok": "pkg:main", "bad": {"nested": True}}}}
        self.assertEqual(self._commands(document, "pyproject.toml"), [("ok", "pkg:main")])


class DeclaredLicenseTests(TestCase):
    """Under what terms the repository may be used.

    The engine probed for a file named `LICENSE` and reported that one exists,
    never what it says -- while every ecosystem carries the identifier in its
    manifest, which this analyzer already parsed.
    """

    def _licence(self, document: Mapping[str, Any], manifest: str) -> str | None:
        return _declared_license(dict(document), manifest)

    def test_each_ecosystem_declares_it_somewhere_different(self) -> None:
        for document, manifest, expected in (
            ({"project": {"license": "AGPL-3.0-only"}}, "pyproject.toml", "AGPL-3.0-only"),
            ({"license": "Apache-2.0"}, "package.json", "Apache-2.0"),
            ({"package": {"license": "MIT OR Apache-2.0"}}, "Cargo.toml", "MIT OR Apache-2.0"),
        ):
            self.assertEqual(self._licence(document, manifest), expected)

    def test_the_pep621_table_form_is_read(self) -> None:
        self.assertEqual(
            self._licence({"project": {"license": {"text": "MIT"}}}, "pyproject.toml"), "MIT"
        )

    def test_the_legacy_npm_array_is_read(self) -> None:
        # `licenses` is a separate key holding a list, not the same key with a
        # different type, so looking only at `license` finds nothing.
        self.assertEqual(self._licence({"licenses": [{"type": "BSD"}]}, "package.json"), "BSD")

    def test_a_manifest_declaring_none_says_none(self) -> None:
        self.assertIsNone(self._licence({"project": {"name": "x"}}, "pyproject.toml"))

    def test_a_pointer_to_a_file_is_not_an_identifier(self) -> None:
        # `{file = "LICENSE"}` says where the terms are, not what they are.
        # Quoting the path as though it were an identifier would report a
        # repository licensed under `LICENSE`.
        self.assertIsNone(
            self._licence({"project": {"license": {"file": "LICENSE"}}}, "pyproject.toml")
        )


class ThirdPartyOriginTests(TestCase):
    """A stylesheet reaches third parties too, and no language analyzer reads one."""

    def _origins(self, name: str, source: str) -> set[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / name).write_text(source, encoding="utf-8")
            result = ProjectMetadataAnalyzer().analyze(scan_repository(root))
        return {
            item.claim.split("third-party origin ")[1].split(",")[0]
            for item in result.claims
            if item.category == "third_party_origin"
        }

    def test_an_import_and_a_background_url_are_both_reported(self) -> None:
        origins = self._origins(
            "app.css",
            "@import url('https://fonts.googleapis.com/css2?family=Lora');\n"
            "body { background-image: url('https://www.transparenttextures.com/p/x.png'); }\n",
        )
        self.assertEqual(origins, {"fonts.googleapis.com", "www.transparenttextures.com"})

    def test_an_xml_namespace_is_not_a_request(self) -> None:
        # Inline SVG declares this on every element. Treating it as egress
        # would flag every icon in the tree.
        self.assertEqual(
            self._origins("icon.css", 'a { content: url("http://www.w3.org/2000/svg"); }\n'), set()
        )

    def test_a_loopback_host_is_not_third_party(self) -> None:
        self.assertEqual(
            self._origins("dev.css", "@import url('http://localhost:3000/x.css');\n"), set()
        )

    def test_one_host_used_twice_is_reported_once(self) -> None:
        origins = self._origins(
            "twice.css",
            "@import url('https://cdn.example.com/a.css');\n"
            "body { background: url('https://cdn.example.com/b.png'); }\n",
        )
        self.assertEqual(origins, {"cdn.example.com"})


class CompilerSettingTests(TestCase):
    """`strict: false` decides the value of every annotation in the tree."""

    def _settings(self, source: str) -> dict[str, str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tsconfig.json").write_text(source, encoding="utf-8")
            result = ProjectMetadataAnalyzer().analyze(scan_repository(root))
        for symbol in result.symbols:
            settings = symbol.metadata.get("config_settings")
            if settings:
                return dict(settings)
        return {}

    def test_nested_settings_flatten_to_dotted_keys(self) -> None:
        settings = self._settings('{"compilerOptions": {"strict": true, "target": "es2017"}}')
        self.assertEqual(settings["compilerOptions.strict"], "true")
        self.assertEqual(settings["compilerOptions.target"], "es2017")

    def test_a_list_setting_is_rendered_as_its_members(self) -> None:
        settings = self._settings('{"compilerOptions": {"lib": ["dom", "esnext"]}}')
        self.assertEqual(settings["compilerOptions.lib"], "dom, esnext")

    def test_comments_and_trailing_commas_are_tolerated(self) -> None:
        # tsconfig is JSONC by convention and real ones use it, so a strict
        # parser would reject perfectly ordinary configurations.
        settings = self._settings(
            '{\n  // the compiler section\n  "compilerOptions": {\n'
            '    "strict": false, /* off */\n  },\n}'
        )
        self.assertEqual(settings["compilerOptions.strict"], "false")

    def test_a_double_slash_inside_a_string_is_not_a_comment(self) -> None:
        settings = self._settings('{"compilerOptions": {"baseUrl": "https://x.test/y"}}')
        self.assertEqual(settings["compilerOptions.baseUrl"], "https://x.test/y")


class DeclaredCommitmentTests(TestCase):
    """What a repository said it must do, which no analyzer can read from code.

    A specification generator recovers what a codebase *does* from the code.
    What it cannot recover that way is what the codebase promised, and that
    lives in prose — a heading naming an obligation group, followed by the
    bullets that spell it out. `## G1: Safe repository boundary` and its three
    lines are a commitment, and the same shape appears in a requirements
    document, a threat model, a contributing guide, and an ADR.

    The idea came from reading another generator's output on this repository.
    It built its entire feature catalogue by reconciling declared documents
    against implementation, and stated the rule it followed: source code is not
    intent, so intent has to be read from where intent was written down.
    """

    def _commitments(self, source: str) -> list[tuple[str, int, int]]:
        return _declared_commitments(source)

    def test_a_heading_with_several_bullets_is_a_commitment(self) -> None:
        source = "## G1: Safe boundary\n\n- No execution.\n- No network.\n- No writes.\n"
        self.assertEqual(self._commitments(source), [("G1: Safe boundary", 3, 1)])

    def test_a_heading_with_one_bullet_is_a_remark(self) -> None:
        # One bullet under a heading is a note, not a list of obligations.
        self.assertEqual(self._commitments("## Notes\n\n- Something.\n"), [])

    def test_numbered_obligations_count(self) -> None:
        source = "## Steps\n\n1. First.\n2. Second.\n3. Third.\n"
        self.assertEqual(self._commitments(source)[0][1], 3)

    def test_bullets_inside_a_code_fence_are_not_obligations(self) -> None:
        source = "## Example\n\n```\n- not an obligation\n- nor this\n```\n"
        self.assertEqual(self._commitments(source), [])

    def test_a_declarative_filename_is_recognized(self) -> None:
        self.assertTrue(is_declarative_document("docs/COMPLETION_GATES.md"))
        self.assertTrue(is_declarative_document("docs/PRODUCT_REQUIREMENTS.md"))

    def test_an_adr_directory_is_recognized(self) -> None:
        # Architecture decision records state a decision and its consequences.
        self.assertTrue(is_declarative_document("docs/decisions/0003-keep-it-local.md"))
        self.assertTrue(is_declarative_document("docs/adr/0001-choose-sqlite.md"))

    def test_a_descriptive_document_is_not_declarative(self) -> None:
        # A README says what a thing does; it does not promise anything.
        self.assertFalse(is_declarative_document("README.md"))
        self.assertFalse(is_declarative_document("docs/BENCHMARK.md"))


class CheckedOutRevisionTests(TestCase):
    """Which revision the bytes came from, which a snapshot identifier cannot say.

    A snapshot is a deterministic function of a directory, not of a commit. Two
    people on the same revision can hold different working trees, so a reader
    with only a snapshot identifier cannot tell which revision it corresponds
    to, and a diff taken on two machines reads as change rather than as
    difference.

    Recording the revision does not make the snapshot commit-addressed and the
    claim says so. It turns "some state of this repository" into "this
    revision, plus whatever was uncommitted", which is the honest version of
    the same fact.
    """

    def test_a_branch_head_is_read(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            git = root / ".git"
            (git / "refs" / "heads").mkdir(parents=True)
            (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (git / "refs" / "heads" / "main").write_text("a" * 40 + "\n", encoding="utf-8")
            self.assertEqual(_checked_out_revision(root), ("a" * 40, "refs/heads/main"))

    def test_a_detached_head_reports_its_commit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("b" * 40 + "\n", encoding="utf-8")
            self.assertEqual(_checked_out_revision(root), ("b" * 40, "detached HEAD"))

    def test_a_packed_ref_is_resolved(self) -> None:
        # A freshly cloned repository keeps its refs packed rather than loose.
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (root / ".git" / "packed-refs").write_text(
                "# pack-refs with: peeled\n" + "c" * 40 + " refs/heads/main\n", encoding="utf-8"
            )
            self.assertEqual(_checked_out_revision(root), ("c" * 40, "refs/heads/main"))

    def test_a_directory_that_is_not_a_repository_reports_nothing(self) -> None:
        with TemporaryDirectory() as directory:
            self.assertIsNone(_checked_out_revision(Path(directory)))

    def test_an_unresolvable_ref_reports_nothing(self) -> None:
        # A HEAD pointing at a branch with no loose or packed ref: say nothing
        # rather than guess at a revision.
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref: refs/heads/gone\n", encoding="utf-8")
            self.assertIsNone(_checked_out_revision(root))


class DeclaredNonGoalTests(TestCase):
    """A non-goal is a declared absence, not an obligation.

    Found by comparing this engine's specification of its own repository with
    another generator's on the same subject. Theirs wrote that an absent
    concern "is a design decision recorded in the repository rather than an
    omission discovered by this document". Ours had read the same heading and
    reported that `docs/PRODUCT_REQUIREMENTS.md` "declares Non-goals with 6
    stated obligation(s)" -- six things the project refuses to build,
    described as promises to build them.

    That is the true-but-misleading shape `open-skeleton audit` exists to
    catch, produced by this engine about itself.
    """

    def test_a_non_goal_heading_is_recognized(self) -> None:
        for heading in ("Non-goals", "Out of scope", "What this deliberately does not do"):
            self.assertTrue(is_non_goal_heading(heading), heading)

    def test_an_obligation_heading_is_not_a_non_goal(self) -> None:
        for heading in (
            "G1: Safe repository boundary",
            "Required outcomes",
            "Before changing code",
        ):
            self.assertFalse(is_non_goal_heading(heading), heading)

    def test_the_two_kinds_are_reported_under_different_categories(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "REQUIREMENTS.md").write_text(
                "## Required outcomes\n\n- One.\n- Two.\n\n## Non-goals\n\n- Never this.\n"
                "- Nor this.\n- Nor that.\n",
                encoding="utf-8",
            )
            result = analyze_snapshot(scan_repository(root))
        found = {claim.category: claim.claim for claim in result.claims}
        self.assertIn("declared_commitment", found)
        self.assertIn("declared_non_goal", found)
        self.assertIn("places 3 thing(s) outside this project", found["declared_non_goal"])
        self.assertIn("stated obligation(s)", found["declared_commitment"])


class CargoManifestTests(TestCase):
    """A Rust repository's manifest was not read at all.

    `Cargo.toml` was never in the eligibility set, so a Rust repository
    reported no dependencies, no package name and no licence -- which is a
    statement about this reader rather than about the repository.
    """

    MANIFEST = """\
[package]
name = "sample-crate"
license = "AGPL-3.0-only"

[dependencies]
serde = "1.0"
tokio = { version = "1", features = ["full"] }
sample-core = { path = "../sample-core" }

[dev-dependencies]
proptest = "1"

[build-dependencies]
cc = "1"
"""

    def _analyze(self, manifest: str) -> AnalysisResult:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Cargo.toml").write_text(manifest, encoding="utf-8")
            return ProjectMetadataAnalyzer().analyze(scan_repository(root))

    def test_registry_dependencies_become_edges(self) -> None:
        result = self._analyze(self.MANIFEST)
        declared = {
            edge.target_ref for edge in result.edges if edge.relationship == "declares_dependency"
        }
        self.assertEqual(declared, {"serde", "tokio", "sample-core", "proptest", "cc"})

    def test_a_path_dependency_is_counted_separately_from_the_supply_chain(self) -> None:
        declared = _cargo_dependencies(
            {
                "dependencies": {
                    "serde": "1.0",
                    "sample-core": {"path": "../sample-core"},
                }
            }
        )
        self.assertEqual(declared["runtime"], {"serde"})
        self.assertEqual(declared["internal"], {"sample-core"})

    def test_inventory_reports_the_path_dependency_without_inflating_the_count(self) -> None:
        result = self._analyze(self.MANIFEST)
        claim = next(item for item in result.claims if item.category == "dependency_inventory")
        self.assertIn("2 runtime", claim.claim)
        self.assertIn("2 optional", claim.claim)
        self.assertIn("A further 1 are declared by path", claim.claim)

    def test_a_renamed_dependency_reports_the_crate_actually_fetched(self) -> None:
        declared = _cargo_dependencies(
            {"dependencies": {"json": {"version": "1", "package": "serde_json"}}}
        )
        self.assertEqual(declared["runtime"], {"serde-json"})

    def test_a_workspace_root_declares_its_shared_dependencies(self) -> None:
        declared = _cargo_dependencies({"workspace": {"dependencies": {"bevy": "0.14"}}})
        self.assertEqual(declared["runtime"], {"bevy"})

    def test_the_package_name_is_recorded_on_the_manifest_symbol(self) -> None:
        self.assertEqual(_cargo_name({"package": {"name": "sample-crate"}}), "sample-crate")
        self.assertIsNone(_cargo_name({"workspace": {"members": ["a"]}}))

    def test_the_declared_licence_is_reported_for_rust_too(self) -> None:
        result = self._analyze(self.MANIFEST)
        claim = next(item for item in result.claims if item.category == "declared_license")
        self.assertIn("`AGPL-3.0-only`", claim.claim)

    def test_a_workspace_declares_the_licence_once_for_every_member(self) -> None:
        self.assertEqual(
            _declared_license(
                {"workspace": {"package": {"license": "AGPL-3.0-only"}}}, "Cargo.toml"
            ),
            "AGPL-3.0-only",
        )

    def test_an_inheritance_marker_is_not_an_identifier(self) -> None:
        # `license.workspace = true` says where to look, not what the terms are,
        # and the root manifest is not this file.
        self.assertIsNone(
            _declared_license({"package": {"license": {"workspace": True}}}, "Cargo.toml")
        )

    def test_a_malformed_manifest_is_recorded_as_a_failure_not_a_guess(self) -> None:
        result = self._analyze("[package\nname = broken")
        self.assertEqual(
            [edge for edge in result.edges if edge.relationship == "declares_dependency"], []
        )
        self.assertTrue(any(item.failures for item in result.coverage))

    def test_a_workspace_root_with_no_dependencies_claims_nothing(self) -> None:
        result = self._analyze('[workspace]\nmembers = ["crates/a"]\n')
        self.assertFalse(any(item.category == "dependency_inventory" for item in result.claims))


class IndexedTextFileTests(TestCase):
    """Files no language analyzer reads still name things.

    A web app describes itself in `.webmanifest` -- name, start URL, theme --
    and that file was Unknown to every reader here. A `.gitignore` names the
    directories a project generates, which the scanner now uses to decide the
    census, so those patterns should be searchable rather than only inferable
    from an exclusion row.
    """

    def _names(self, filename: str, body: str) -> set[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / filename).write_text(body, encoding="utf-8")
            result = ProjectMetadataAnalyzer().analyze(scan_repository(root))
            indexed = [item for item in result.symbols if item.kind == "text_names"]
            return {name for item in indexed for name in item.metadata["name_index"]}

    def test_a_web_manifest_is_indexed(self) -> None:
        names = self._names(
            "manifest.webmanifest",
            '{"short_name": "Mud", "start_url": "/", "theme_color": "#000"}',
        )
        self.assertIn("short_name", names)
        self.assertIn("start_url", names)

    def test_a_gitignore_is_indexed(self) -> None:
        names = self._names(".gitignore", "__pycache__\npoetry.lock\nyarn.lock\n")
        self.assertIn("poetry.lock", names)
        self.assertIn("yarn.lock", names)

    def test_a_file_no_reader_claims_is_still_not_indexed(self) -> None:
        # The set is a decision about which files name things, not a licence
        # to index every byte in the repository.
        self.assertEqual(self._names("notes.rst", "anything_at_all\n"), set())
