# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from open_skeleton.dashboard import create_dashboard_server
from open_skeleton.mcp_server import OpenSkeletonService


class DashboardTests(TestCase):
    def test_serves_local_read_only_summary_claims_and_coverage(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            state = workspace / "state"
            root.mkdir()
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            OpenSkeletonService(root, state).refresh_analysis()
            server = create_dashboard_server(root, state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(f"{base}/", timeout=5) as response:
                    html = response.read().decode("utf-8")
                    self.assertIn("Open Skeleton", html)
                    self.assertIn("Content-Security-Policy", response.headers)
                    self.assertNotIn("unsafe-inline", response.headers["Content-Security-Policy"])
                    self.assertNotIn(' style="', html)
                    self.assertNotIn(".style.", html)
                    self.assertIn("document.createElement('progress')", html)
                with urlopen(f"{base}/api/summary", timeout=5) as response:
                    summary = json.loads(response.read())
                    self.assertGreater(summary["claim_count"], 0)
                with urlopen(f"{base}/api/coverage", timeout=5) as response:
                    self.assertTrue(json.loads(response.read()))
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(Request(f"{base}/api/summary", method="POST"), timeout=5)
                self.assertEqual(rejected.exception.code, 405)
                with self.assertRaises(HTTPError) as rejected_host:
                    urlopen(
                        Request(f"{base}/api/summary", headers={"Host": "evil.example"}),
                        timeout=5,
                    )
                self.assertEqual(rejected_host.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_rejects_non_loopback_bind(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "loopback"):
                create_dashboard_server(root, host="0.0.0.0", port=0)
