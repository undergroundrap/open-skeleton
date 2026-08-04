# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.analyzers.project_metadata import ProjectMetadataAnalyzer
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
            (root / "scripts" / "smoke.py").write_text(
                "import requests\n", encoding="utf-8"
            )
            (root / "backend" / "requirements.txt").write_text(
                "fastapi\n", encoding="utf-8"
            )
            (root / "README.md").write_text(
                "| `GET` | `/health` | documented |\n", encoding="utf-8"
            )

            result = analyze_snapshot(scan_repository(root))
            categories = {item.category for item in result.claims}

            self.assertIn("api_documentation_drift", categories)
            self.assertIn("dependency_drift", categories)
