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
                scan_result = main(
                    ["scan", str(root), "--state-dir", str(state), "--json"]
                )
            self.assertEqual(scan_result, 0)
            summary = json.loads(scan_stdout.getvalue())
            self.assertEqual(summary["file_count"], 5)
            self.assertIn("[complete]", scan_stderr.getvalue())

            status_stdout = StringIO()
            with redirect_stdout(status_stdout):
                status_result = main(
                    ["status", str(root), "--state-dir", str(state), "--json"]
                )
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
