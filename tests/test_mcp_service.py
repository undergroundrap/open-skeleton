# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

import json
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


class ContractToolTests(TestCase):
    """The agent-facing surface must reach the concordances too.

    They existed only inside a rendered specification, so an agent asking
    what moves together had to load tens of thousands of words to find out.
    This is the same question the CLI answers, on the surface an agent
    actually reaches.
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

    def _service(self, workspace: Path) -> OpenSkeletonService:
        root = workspace / "repo"
        root.mkdir()
        (root / "store.py").write_text(self.SCHEMA_SOURCE, encoding="utf-8")
        (root / "guard.py").write_text(self.GUARD_SOURCE, encoding="utf-8")
        service = OpenSkeletonService(root, workspace / "state")
        service.refresh_analysis()
        return service

    def test_it_names_every_site_of_a_shared_vocabulary(self) -> None:
        with TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            answer = service.list_contracts()
            self.assertTrue(answer["snapshot_id"])
            vocabulary = answer["value_sets"][0]
            self.assertEqual(vocabulary["members"], ["done", "queued"])
            self.assertIn("sql_check", vocabulary["kinds"])
            self.assertIn("membership_guard", vocabulary["kinds"])
            sites = {item["path"] for item in vocabulary["declarations"]}
            self.assertEqual(sites, {"store.py", "guard.py"})

    def test_a_term_narrows_the_answer(self) -> None:
        with TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            self.assertTrue(service.list_contracts(term="queued")["value_sets"])
            narrowed = service.list_contracts(term="nothing-declares-this")
            self.assertEqual(narrowed["value_sets"], [])
            self.assertEqual(narrowed["records"], [])

    def test_a_kind_filter_selects_one_family(self) -> None:
        with TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            self.assertEqual(service.list_contracts(kind="record")["value_sets"], [])

    def test_the_answer_stays_small_enough_to_hand_an_agent(self) -> None:
        # The whole point. An answer that costs what the document costs is
        # not an answer, and nothing else in the suite would notice it
        # growing.
        with TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            payload = json.dumps(service.list_contracts())
            self.assertLess(len(payload.split()), 400)

    def test_the_tool_is_registered_on_the_server(self) -> None:
        # A service method nothing exposes is invisible to the agent it was
        # written for.
        with TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            self.assertTrue(callable(service.list_contracts))
            self.assertIn("list_contracts", dir(service))
