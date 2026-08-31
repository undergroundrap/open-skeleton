# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository
from open_skeleton.spec import build_spec, load_profile
from open_skeleton.spec.concordance import build_contract_concordance


def _claim(
    claim_id: str,
    category: str,
    text: str,
    *evidence_ids: str,
) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "category": category,
        "claim": text,
        "supporting_evidence": list(evidence_ids),
    }


class ContractConcordanceTests(TestCase):
    def test_exact_surfaces_and_dynamic_prefixes_are_joined(self) -> None:
        claims = (
            _claim("route-get", "http_route", "GET /items is handled by api.items.", "e-get"),
            _claim(
                "route-item",
                "http_route",
                "GET /items/{item_id} is handled by api.item.",
                "e-item",
            ),
            _claim(
                "client-items",
                "http_client_route",
                "http://localhost:8000/items is requested by web.client; the server side is "
                "unresolved.",
                "e-client",
            ),
            _claim(
                "client-item",
                "http_client_route_prefix",
                "http://127.0.0.1:8000/items/ begins a request path built by web.client, whose "
                "remaining segments are interpolated at run time; the server side is unresolved.",
                "e-prefix",
            ),
            _claim(
                "external-items",
                "http_client_route",
                "https://example.com/items is requested by web.client; the server side is "
                "unresolved.",
                "e-external",
            ),
            _claim(
                "docs",
                "documented_http_route_inventory",
                "Markdown API tables document 2 distinct HTTP method/path endpoints.",
                "e-doc-get",
                "e-doc-item",
            ),
        )
        evidence = {
            "e-doc-get": {
                "evidence_id": "e-doc-get",
                "evidence_kind": "documented_http_route",
                "symbol": "GET /items",
            },
            "e-doc-item": {
                "evidence_id": "e-doc-item",
                "evidence_kind": "documented_http_route",
                "symbol": "GET /items/{item_id}",
            },
        }

        rows = build_contract_concordance(
            snapshot_id="snapshot",
            claims=claims,
            evidence_by_id=evidence,
        )
        by_path = {item.path: item for item in rows}

        self.assertEqual(by_path["/items"].served_methods, ("GET",))
        self.assertEqual(by_path["/items"].documented_methods, ("GET",))
        self.assertEqual(by_path["/items"].documentation_relation, "method/path agree")
        self.assertEqual(by_path["/items"].client_relation, "exact path requested in repository")
        self.assertNotIn("e-external", by_path["/items"].evidence_ids)
        self.assertEqual(
            by_path["/items/{item_id}"].client_relation,
            "compatible static prefix requested in repository",
        )
        self.assertIn("e-prefix", by_path["/items/{item_id}"].evidence_ids)

    def test_nearby_paths_and_conflicting_methods_are_not_collapsed(self) -> None:
        claims = (
            _claim("route", "http_route", "POST /user is registered as a route in api.", "e1"),
            _claim(
                "client",
                "http_client_route",
                "/users is requested by web.client; the server side is unresolved.",
                "e2",
            ),
            _claim(
                "prefix",
                "http_client_route_prefix",
                "/orders/ begins a request path built by web.client, whose remaining segments "
                "are interpolated at run time; the server side is unresolved.",
                "e3",
            ),
            _claim(
                "near-prefix",
                "http_client_route_prefix",
                "/item begins a request path built by web.client, whose remaining segments are "
                "interpolated at run time; the server side is unresolved.",
                "e-near-prefix",
            ),
            _claim(
                "docs",
                "documented_http_route_inventory",
                "Markdown API tables document 1 distinct HTTP method/path endpoints.",
                "e-doc",
            ),
        )
        evidence = {
            "e-doc": {
                "evidence_id": "e-doc",
                "evidence_kind": "documented_http_route",
                "symbol": "GET /user",
            }
        }

        rows = build_contract_concordance(
            snapshot_id="snapshot",
            claims=claims,
            evidence_by_id=evidence,
        )
        by_path = {item.path: item for item in rows}

        self.assertEqual(by_path["/user"].documentation_relation, "path agrees; methods conflict")
        self.assertEqual(by_path["/user"].client_relation, "no in-repository caller observed")
        self.assertEqual(by_path["/users"].served_methods, ())
        self.assertNotIn("e-near-prefix", by_path["/users"].evidence_ids)
        self.assertEqual(
            by_path["/users"].documentation_relation,
            "no served or documented route observed",
        )
        self.assertEqual(
            by_path["/orders/"].client_relation,
            "dynamic request prefix has no compatible route in this snapshot",
        )
        self.assertEqual(
            by_path["/item"].client_relation,
            "dynamic request prefix has no compatible route in this snapshot",
        )

    def test_full_pipeline_exposes_complete_concordance_and_panel(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            (root / "README.md").write_text(
                "# Service\n\n| Method | Path |\n|---|---|\n| `GET` | `/api/items` |\n",
                encoding="utf-8",
            )
            (root / "server.py").write_text(
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/api/items')\n"
                "def items():\n"
                "    return []\n",
                encoding="utf-8",
            )
            (root / "client.ts").write_text(
                "export async function items() { return fetch('/api/items'); }\n",
                encoding="utf-8",
            )
            snapshot = scan_repository(root)
            analysis = analyze_snapshot(snapshot)
            ledger = EvidenceLedger(workspace / "state" / "evidence.sqlite3")
            ledger.save_snapshot(snapshot)
            ledger.save_analysis(analysis)

            document = build_spec(ledger, load_profile())
            row = next(item for item in document.contract_concordance if item.path == "/api/items")
            http = next(item for item in document.sections if item.section_id == "surface.http")
            panel = next(item for item in http.panels if item.name == "contract_concordance")

            self.assertEqual(row.served_methods, ("GET",))
            self.assertEqual(row.documented_methods, ("GET",))
            self.assertEqual(row.client_relation, "exact path requested in repository")
            self.assertTrue(row.claim_ids)
            self.assertTrue(row.evidence_ids)
            self.assertTrue(panel.rows)
