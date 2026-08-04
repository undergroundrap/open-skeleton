# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.mcp_server import OpenSkeletonService


class McpServiceTests(TestCase):
    def test_repository_bound_service_refreshes_and_queries_evidence(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            (root / "app.py").write_text(
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/ready')\n"
                "def ready(): return {'ready': True}\n",
                encoding="utf-8",
            )
            service = OpenSkeletonService(root, workspace / "state")

            refreshed = service.refresh_analysis()
            claims = service.search_claims("ready")
            all_claims = service.list_claims(category="http_route")
            coverage = service.analysis_coverage()
            receipt = service.get_evidence(all_claims[0]["supporting_evidence"][0])
            symbols = service.list_symbols(query="ready")
            context = service.build_context_pack("ready", max_chars=5_000)

            self.assertTrue(refreshed["ledger_write"])
            self.assertFalse(refreshed["target_repository_write"])
            self.assertTrue(claims)
            self.assertTrue(coverage)
            self.assertEqual(receipt["excerpt_status"], "verified-current-by-file-hash")
            self.assertIn("@app.get", receipt["excerpt"])
            self.assertTrue(symbols)
            self.assertTrue(context["claims"])

    def test_service_cannot_be_redirected_to_another_root_by_tool_arguments(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            service = OpenSkeletonService(root, Path(temporary) / "state")

            self.assertEqual(service.root, root.resolve())
            self.assertNotIn("root", service.refresh_analysis.__annotations__)
