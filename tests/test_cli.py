# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from open_skeleton.cli import _coverage_report, _refusal_rows, main
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


class RefusalRowTests(TestCase):
    """What a symbol refuses with, and the words it uses.

    A status code says a request failed. The message says which failure it
    was, and it is the string somebody searches for when it turns up in a
    log. Both were in the ledger; reaching them meant reading a rendered
    specification or the source.
    """

    def _rows(self, *symbols: dict[str, Any]) -> list[dict[str, Any]]:
        return _refusal_rows(list(symbols))

    def _symbol(self, name: str, *events: dict[str, Any]) -> dict[str, Any]:
        return {
            "qualified_name": name,
            "path": "main.py",
            "start_line": 10,
            "metadata": {"control_flow": list(events)},
        }

    def test_a_refusal_carries_its_status_and_message(self) -> None:
        rows = self._rows(
            self._symbol(
                "load_player",
                {"kind": "raise", "line": 12, "label": "HTTP 404", "message": "Player not found"},
            )
        )
        self.assertEqual(rows[0]["symbol"], "load_player")
        refusal = rows[0]["refusals"][0]
        self.assertEqual(refusal["label"], "HTTP 404")
        self.assertEqual(refusal["message"], "Player not found")
        self.assertEqual(refusal["line"], 12)

    def test_a_refusal_without_a_literal_message_still_reports_its_status(self) -> None:
        rows = self._rows(self._symbol("go", {"kind": "raise", "line": 3, "label": "HTTP 400"}))
        self.assertIsNone(rows[0]["refusals"][0]["message"])

    def test_a_bare_reraise_is_not_a_refusal(self) -> None:
        # It says the failure continues outward, which the caller already
        # learns from the site that produced it.
        rows = self._rows(self._symbol("go", {"kind": "raise", "line": 3, "label": "re-raise"}))
        self.assertEqual(rows, [])

    def test_guards_and_returns_are_not_refusals(self) -> None:
        rows = self._rows(
            self._symbol(
                "go",
                {"kind": "guard", "line": 2, "label": "if x"},
                {"kind": "return", "line": 4, "label": "None"},
            )
        )
        self.assertEqual(rows, [])

    def test_a_symbol_with_no_control_flow_is_skipped(self) -> None:
        self.assertEqual(self._rows({"qualified_name": "x", "path": "a.py"}), [])

    def test_refusals_are_ordered_by_line(self) -> None:
        rows = self._rows(
            self._symbol(
                "go",
                {"kind": "raise", "line": 9, "label": "HTTP 500"},
                {"kind": "raise", "line": 4, "label": "HTTP 404"},
            )
        )
        self.assertEqual([item["line"] for item in rows[0]["refusals"]], [4, 9])


class OutputEncodingTests(TestCase):
    """Answers are written as UTF-8 whatever the console encoding is.

    An em dash left as the single cp1252 byte `\\x97` made a quoted message
    stop being the string it quoted, for any caller decoding as UTF-8 --
    which is what a caller does. A message that arrives mangled is worse
    than one that never arrived, because it looks usable.
    """

    def test_a_non_ascii_message_survives_the_round_trip(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            state = Path(temporary) / "state"
            root.mkdir()
            # A route handler, because the guard-and-exit trace is kept
            # for those. A plain function raising once carries no trace,
            # which is the command's real scope rather than a gap here.
            (root / "api.py").write_text(
                "from fastapi import FastAPI, HTTPException\n"
                "app = FastAPI()\n"
                "@app.get('/zone')\n"
                "def load(zone_id: str):\n"
                "    if not zone_id:\n"
                '        raise HTTPException(status_code=400, detail="zone id required")\n'
                "    if zone_id == 'missing':\n"
                '        raise HTTPException(status_code=404, detail="zone not found \u2014 data may be corrupt")\n'
                "    return {'zone': zone_id}\n",
                encoding="utf-8",
            )
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                main(["analyze", str(root), "--state-dir", str(state)])
            captured = StringIO()
            with redirect_stdout(captured), redirect_stderr(StringIO()):
                main(["refusals", str(root), "--state-dir", str(state)])
            output = captured.getvalue()
            self.assertIn("\u2014", output)
            self.assertEqual(output.encode("utf-8").decode("utf-8"), output)


class CoverageReportTests(TestCase):
    """Whether an absence can be trusted.

    "This repository does not authenticate" and "the files that would have
    shown it were never opened" produce the same silence. The difference is
    the whole question an auditor is asking, and it was only answerable by
    reading a rendered document.
    """

    def _report(
        self,
        files: list[dict[str, Any]],
        exclusions: list[dict[str, Any]] | None = None,
        symbols: list[dict[str, Any]] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        coverage: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return _coverage_report(
            files, exclusions or [], symbols or [], evidence or [], coverage or []
        )

    def test_a_directory_exclusion_counts_the_files_it_took(self) -> None:
        # One row for a build cache is one row, and thousands of files.
        report = self._report(
            [{"path": "a.py", "language": "Python"}],
            exclusions=[{"reason": "gitignored:target/", "contained_files": 15912}],
        )
        self.assertEqual(report["excluded_files"], 15912)
        self.assertEqual(report["exclusion_reasons"]["gitignored:target/"], 15912)

    def test_a_single_excluded_file_counts_as_one(self) -> None:
        report = self._report(
            [],
            exclusions=[{"reason": "known-binary-type", "contained_files": 0}],
        )
        self.assertEqual(report["excluded_files"], 1)

    def test_a_language_nothing_touched_is_named(self) -> None:
        report = self._report(
            [
                {"path": "a.py", "language": "Python"},
                {"path": "run.sh", "language": "Shell"},
            ],
            symbols=[{"path": "a.py"}],
        )
        self.assertEqual(report["languages_no_analyzer_read"], [{"language": "Shell", "files": 1}])

    def test_a_file_reached_only_by_a_receipt_counts_as_read(self) -> None:
        report = self._report(
            [{"path": "a.md", "language": "Markdown"}],
            evidence=[{"path": "a.md"}],
        )
        self.assertEqual(report["languages_no_analyzer_read"], [])

    def test_an_eligible_but_unparsed_language_is_reported_once(self) -> None:
        # Its analyzer claimed it and failed, which is a parse shortfall with
        # a reason attached. Naming it again as unread states one cause
        # twice and reads as two.
        report = self._report(
            [{"path": "a.hum", "language": "Hum"}],
            coverage=[
                {
                    "analyzer": "hum-semantic-index/v1",
                    "language": "Hum",
                    "eligible_files": 1,
                    "analyzed_files": 0,
                    "failures": ["needs a pre-generated index"],
                }
            ],
        )
        self.assertEqual(report["languages_no_analyzer_read"], [])
        self.assertEqual(len(report["eligible_but_unparsed"]), 1)
        self.assertEqual(report["eligible_but_unparsed"][0]["analyzed_files"], 0)

    def test_a_fully_parsed_analyzer_is_not_a_shortfall(self) -> None:
        report = self._report(
            [{"path": "a.py", "language": "Python"}],
            symbols=[{"path": "a.py"}],
            coverage=[
                {
                    "analyzer": "python-ast/v2",
                    "language": "Python",
                    "eligible_files": 1,
                    "analyzed_files": 1,
                    "failures": [],
                }
            ],
        )
        self.assertEqual(report["eligible_but_unparsed"], [])

    def test_the_command_reports_a_clean_read_rather_than_saying_nothing(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            state = Path(temporary) / "state"
            root.mkdir()
            (root / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                main(["analyze", str(root), "--state-dir", str(state)])
            captured = StringIO()
            with redirect_stdout(captured), redirect_stderr(StringIO()):
                main(["coverage", str(root), "--state-dir", str(state)])
            output = captured.getvalue()
            self.assertIn("included files:", output)
            self.assertIn("read by an analyzer equipped for it", output)


class SnapshotDriftTests(TestCase):
    """An answer about a repository that has since moved on.

    Every query answers from a stored snapshot, and nothing checked that the
    snapshot still described the files on disk. Editing a repository and
    asking a question returned a confident report about a state that no
    longer existed. A stale answer is worse than no answer, because it looks
    like an answer.
    """

    def _repo(self, workspace: Path) -> tuple[Path, Path]:
        root = workspace / "repo"
        state = workspace / "state"
        root.mkdir()
        (root / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            main(["analyze", str(root), "--state-dir", str(state)])
        return root, state

    def _ask(self, root: Path, state: Path, *argv: str) -> str:
        captured = StringIO()
        with redirect_stdout(captured), redirect_stderr(StringIO()):
            main([argv[0], str(root), "--state-dir", str(state), *argv[1:]])
        return captured.getvalue()

    def test_a_fresh_snapshot_carries_no_warning(self) -> None:
        with TemporaryDirectory() as temporary:
            root, state = self._repo(Path(temporary))
            self.assertNotIn("no longer matches", self._ask(root, state, "coverage"))

    def test_an_added_file_makes_the_answer_stale(self) -> None:
        with TemporaryDirectory() as temporary:
            root, state = self._repo(Path(temporary))
            (root / "b.py").write_text("def go():\n    pass\n", encoding="utf-8")
            output = self._ask(root, state, "coverage")
            self.assertIn("no longer matches", output)
            self.assertIn("1 added", output)
            self.assertIn("b.py", output)

    def test_a_removed_file_makes_the_answer_stale(self) -> None:
        with TemporaryDirectory() as temporary:
            root, state = self._repo(Path(temporary))
            (root / "a.py").unlink()
            self.assertIn("1 removed", self._ask(root, state, "coverage"))

    def test_a_changed_file_makes_the_answer_stale(self) -> None:
        with TemporaryDirectory() as temporary:
            root, state = self._repo(Path(temporary))
            (root / "a.py").write_text("VALUE = 22222\n", encoding="utf-8")
            self.assertIn("1 changed", self._ask(root, state, "coverage"))

    def test_an_edit_that_keeps_the_byte_count_is_still_stale(self) -> None:
        # Same paths and sizes, different digest. Reporting "0 changed"
        # would be a worse answer than saying the contents moved.
        with TemporaryDirectory() as temporary:
            root, state = self._repo(Path(temporary))
            (root / "a.py").write_text("VALUE = 9\n", encoding="utf-8")
            output = self._ask(root, state, "coverage")
            self.assertIn("no longer matches", output)
            self.assertIn("content changed", output)

    def test_the_warning_reaches_a_caller_capturing_the_answer(self) -> None:
        # On stderr an agent piping stdout would never see it, which is how
        # a caveat becomes decoration.
        with TemporaryDirectory() as temporary:
            root, state = self._repo(Path(temporary))
            (root / "b.py").write_text("x = 1\n", encoding="utf-8")
            captured, errors = StringIO(), StringIO()
            with redirect_stdout(captured), redirect_stderr(errors):
                main(["contracts", str(root), "--state-dir", str(state)])
            self.assertIn("no longer matches", captured.getvalue())

    def test_json_carries_the_warning_as_a_field(self) -> None:
        with TemporaryDirectory() as temporary:
            root, state = self._repo(Path(temporary))
            (root / "b.py").write_text("x = 1\n", encoding="utf-8")
            payload = json.loads(self._ask(root, state, "refusals", "--json"))
            self.assertIsNotNone(payload["stale"])
            self.assertIn("no longer matches", payload["stale"])

    def test_a_fresh_json_answer_says_so_explicitly(self) -> None:
        with TemporaryDirectory() as temporary:
            root, state = self._repo(Path(temporary))
            payload = json.loads(self._ask(root, state, "contracts", "--json"))
            self.assertIsNone(payload["stale"])

    def test_a_cold_start_names_the_command_that_fixes_it(self) -> None:
        # A dead end helps nobody, least of all something that has not read
        # the README.
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            (root / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
            errors = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(errors):
                code = main(["contracts", str(root), "--state-dir", str(Path(temporary) / "none")])
            self.assertNotEqual(code, 0)
            self.assertIn("analyze", errors.getvalue())
