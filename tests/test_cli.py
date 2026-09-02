# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.cli import main
from tests.helpers import create_sample_repository


class CliTests(TestCase):
    def test_scan_and_status_commands(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            state = Path(temporary) / "state"
            root.mkdir()
            create_sample_repository(root)

            scan_stdout = StringIO()
            scan_stderr = StringIO()
            with redirect_stdout(scan_stdout), redirect_stderr(scan_stderr):
                scan_result = main(["scan", str(root), "--state-dir", str(state), "--json"])
            self.assertEqual(scan_result, 0)
            summary = json.loads(scan_stdout.getvalue())
            self.assertEqual(summary["file_count"], 5)
            self.assertIn("[complete]", scan_stderr.getvalue())

            status_stdout = StringIO()
            with redirect_stdout(status_stdout):
                status_result = main(["status", str(root), "--state-dir", str(state), "--json"])
            self.assertEqual(status_result, 0)
            status = json.loads(status_stdout.getvalue())
            self.assertEqual(status["snapshot_id"], summary["snapshot_id"])

    def test_invalid_max_file_size_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stderr = StringIO()
            with redirect_stderr(stderr):
                result = main(["scan", str(root), "--max-file-bytes", "0"])
            self.assertEqual(result, 2)
            self.assertIn("must be positive", stderr.getvalue())

    def test_analyze_and_claim_query_commands(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            state = Path(temporary) / "state"
            root.mkdir()
            (root / "app.py").write_text(
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/ready')\n"
                "def ready():\n"
                "    return {'ready': True}\n",
                encoding="utf-8",
            )
            analyze_stdout = StringIO()
            with redirect_stdout(analyze_stdout), redirect_stderr(StringIO()):
                result = main(
                    [
                        "analyze",
                        str(root),
                        "--state-dir",
                        str(state),
                        "--quiet",
                        "--json",
                    ]
                )
            self.assertEqual(result, 0)
            summary = json.loads(analyze_stdout.getvalue())
            self.assertGreater(summary["claim_count"], 0)

            claims_stdout = StringIO()
            with redirect_stdout(claims_stdout):
                result = main(
                    [
                        "claims",
                        str(root),
                        "--state-dir",
                        str(state),
                        "--category",
                        "http_route",
                        "--json",
                    ]
                )
            self.assertEqual(result, 0)
            claims = json.loads(claims_stdout.getvalue())
            self.assertEqual(claims[0]["claim"], "GET /ready is handled by app.ready.")

    def test_spec_command_writes_both_projections_and_verifies(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            state = Path(temporary) / "state"
            root.mkdir()
            create_sample_repository(root)

            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    main(["analyze", str(root), "--state-dir", str(state), "--quiet"]), 0
                )

            spec_stdout = StringIO()
            with redirect_stdout(spec_stdout):
                result = main(["spec", str(root), "--state-dir", str(state), "--verify", "--json"])
            self.assertEqual(result, 0)
            summary = json.loads(spec_stdout.getvalue())

            self.assertEqual(summary["citation_integrity"], 1.0)
            self.assertGreater(summary["verdicts"]["absent"], 0)
            self.assertGreater(summary["verdicts"]["applicable"], 0)
            self.assertEqual(summary["cited_claims"], summary["total_claims"])
            self.assertTrue(Path(summary["markdown"]).is_file())
            self.assertTrue(Path(summary["json"]).is_file())

    def test_spec_verify_fails_when_a_cited_source_changed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            state = Path(temporary) / "state"
            root.mkdir()
            create_sample_repository(root)

            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                main(["analyze", str(root), "--state-dir", str(state), "--quiet"])
            # package.json is cited by the dependency inventory, so editing it must
            # surface as a failing citation rather than pass silently.
            (root / "package.json").write_text(
                '{"name":"sample","version":"2.0.0"}\n', encoding="utf-8"
            )

            with redirect_stdout(StringIO()):
                result = main(["spec", str(root), "--state-dir", str(state), "--verify", "--json"])
            self.assertEqual(result, 1)

    def test_plan_synthesis_writes_parallel_jobs_without_contacting_a_model(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            state = Path(temporary) / "state"
            output = Path(temporary) / "plan.json"
            root.mkdir()
            create_sample_repository(root)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    main(["analyze", str(root), "--state-dir", str(state), "--quiet"]), 0
                )

            stdout = StringIO()
            with redirect_stdout(stdout):
                result = main(
                    [
                        "plan-synthesis",
                        str(root),
                        "--state-dir",
                        str(state),
                        "--output",
                        str(output),
                        "--json",
                    ]
                )

            self.assertEqual(result, 0)
            summary = json.loads(stdout.getvalue())
            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(summary["contacts_model"])
            self.assertGreater(summary["job_count"], 0)
            self.assertEqual(plan["job_count"], summary["job_count"])
            self.assertTrue(all(item["parallel_safe"] for item in plan["jobs"]))

    def test_run_synthesis_plan_requires_execute_before_contacting_provider(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            state = Path(temporary) / "state"
            root.mkdir()
            create_sample_repository(root)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    main(["analyze", str(root), "--state-dir", str(state), "--quiet"]), 0
                )
                self.assertEqual(main(["plan-synthesis", str(root), "--state-dir", str(state)]), 0)

            stdout = StringIO()
            with redirect_stdout(stdout):
                result = main(
                    [
                        "run-synthesis-plan",
                        str(root),
                        "--state-dir",
                        str(state),
                        "--provider",
                        "codex",
                        "--json",
                    ]
                )

            self.assertEqual(result, 0)
            summary = json.loads(stdout.getvalue())
            self.assertFalse(summary["execute"])
            self.assertEqual(summary["status_counts"], {"planned": summary["job_count"]})
            self.assertFalse((state / "synthesis-runs").exists())

    def test_plan_synthesis_rejects_source_derived_output_inside_git(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            state = workspace / "state"
            worktree = workspace / "other-worktree"
            root.mkdir()
            worktree.mkdir()
            (worktree / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
            create_sample_repository(root)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    main(["analyze", str(root), "--state-dir", str(state), "--quiet"]), 0
                )

            stderr = StringIO()
            with redirect_stderr(stderr):
                result = main(
                    [
                        "plan-synthesis",
                        str(root),
                        "--state-dir",
                        str(state),
                        "--output",
                        str(worktree / "synthesis-plan.json"),
                    ]
                )
            self.assertEqual(result, 2)
            self.assertIn("outside Git worktrees", stderr.getvalue())


class ContractsCommandTests(TestCase):
    """Asking what moves together, without loading the document that says so.

    The concordances lived only inside `spec.json`, so learning that a
    vocabulary is declared in five places meant loading a hundred thousand
    words. That is the difference between a specification a team publishes
    and one an agent can ask.
    """

    SCHEMA_SOURCE = (
        'SCHEMA = "CREATE TABLE job ('
        "state TEXT NOT NULL CHECK (state IN ('queued', 'done')), "
        'owner TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);"\n'
    )
    GUARD_SOURCE = (
        "def check(state):\n"
        "    if state not in {'queued', 'done'}:\n"
        "        raise ValueError(state)\n"
    )

    def _run(self, *argv: str) -> str:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            state = Path(temporary) / "state"
            root.mkdir()
            (root / "store.py").write_text(self.SCHEMA_SOURCE, encoding="utf-8")
            (root / "guard.py").write_text(self.GUARD_SOURCE, encoding="utf-8")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                main(["analyze", str(root), "--state-dir", str(state)])
            captured = StringIO()
            with redirect_stdout(captured), redirect_stderr(StringIO()):
                main(["contracts", str(root), "--state-dir", str(state), *argv])
            return captured.getvalue()

    def test_it_names_every_site_of_a_shared_vocabulary(self) -> None:
        output = self._run()
        self.assertIn("value set: done, queued", output)
        self.assertIn("sql_check", output)
        self.assertIn("membership_guard", output)

    def test_the_answer_is_small_enough_to_hand_an_agent(self) -> None:
        # The point of the command. `spec.md` for this engine is ~34,700
        # words; an answer that costs the same is not an answer.
        self.assertLess(len(self._run().split()), 200)

    def test_a_term_narrows_to_the_contract_asked_about(self) -> None:
        self.assertIn("done, queued", self._run("--term", "queued"))

    def test_a_term_matching_nothing_says_so_rather_than_printing_nothing(self) -> None:
        output = self._run("--term", "nothing-declares-this")
        self.assertIn("No contract declared in more than one form", output)

    def test_a_kind_filter_selects_one_family(self) -> None:
        self.assertNotIn("value set:", self._run("--kind", "record"))

    def test_json_output_is_machine_readable(self) -> None:
        payload = json.loads(self._run("--json"))
        self.assertIn("value_sets", payload)
        self.assertIn("records", payload)
        self.assertTrue(payload["snapshot_id"])
        self.assertEqual(payload["value_sets"][0]["members"], ["done", "queued"])
