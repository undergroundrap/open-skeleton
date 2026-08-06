# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from typing import Any
from unittest import TestCase

from open_skeleton.spec.capabilities import (
    Capability,
    _short_name,
    build_capabilities,
    verifying_paths,
)


def _claim(
    claim_id: str, category: str, text: str, evidence: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "category": category,
        "claim": text,
        "supporting_evidence": list(evidence),
    }


class CapabilityClusteringTests(TestCase):
    FILES = (
        {"path": "backend/main.py", "role": "source"},
        {"path": "backend/util.py", "role": "source"},
        {"path": "tests/test_api.py", "role": "test"},
        {"path": "scripts/smoke.py", "role": "source"},
    )
    SYMBOLS = (
        {
            "qualified_name": "backend.main.attack",
            "kind": "async_function",
            "path": "backend/main.py",
        },
        {
            "qualified_name": "backend.main.move",
            "kind": "async_function",
            "path": "backend/main.py",
        },
        {
            "qualified_name": "backend.main.get_player",
            "kind": "async_function",
            "path": "backend/main.py",
        },
        {"qualified_name": "backend.util.helper", "kind": "function", "path": "backend/util.py"},
        {"qualified_name": "scripts.smoke.run", "kind": "function", "path": "scripts/smoke.py"},
    )
    CLAIMS = (
        _claim(
            "r1",
            "http_route",
            "POST /action/attack/{player_id} is handled by backend.main.attack.",
            ("e1",),
        ),
        _claim(
            "r2",
            "http_route",
            "POST /action/move/{player_id} is handled by backend.main.move.",
            ("e2",),
        ),
        _claim(
            "r3",
            "http_route",
            "GET /player/{player_id} is handled by backend.main.get_player.",
            ("e3",),
        ),
        _claim("h1", "operator_harness", "scripts/smoke.py is an operator harness.", ("e4",)),
    )
    EVIDENCE = {
        "e1": {"path": "backend/main.py"},
        "e2": {"path": "backend/main.py"},
        "e3": {"path": "backend/main.py"},
        "e4": {"path": "scripts/smoke.py"},
    }

    def _build(self, edges: tuple[dict[str, Any], ...]) -> tuple[Capability, ...]:
        return build_capabilities(
            files=self.FILES,
            claims=self.CLAIMS,
            symbols=self.SYMBOLS,
            edges=edges,
            evidence_by_id=self.EVIDENCE,
        )

    def test_routes_cluster_by_leading_static_segment(self) -> None:
        capabilities = self._build(())
        labels = {item.label: item for item in capabilities if item.kind == "route-group"}
        self.assertEqual(set(labels), {"action", "player"})
        self.assertEqual(len(labels["action"].routes), 2)
        self.assertEqual(len(labels["player"].routes), 1)

    def test_identifiers_are_sequential_with_no_gaps(self) -> None:
        capabilities = self._build(())
        self.assertEqual(
            [item.capability_id for item in capabilities],
            [f"C-{index:03d}" for index in range(1, len(capabilities) + 1)],
        )

    def test_route_literal_prefix_matches_an_interpolated_client_url(self) -> None:
        # A client builds "/action/attack/{pid}" with an f-string, so the recorded
        # literal is only the static prefix.
        capabilities = self._build(
            (
                {
                    "relationship": "references_route_path",
                    "source_path": "scripts/smoke.py",
                    "target_ref": "/action/attack/",
                },
            )
        )
        action = next(item for item in capabilities if item.label == "action")
        self.assertEqual(action.verification, "exercised")
        self.assertTrue(
            any("requests /action/attack/{player_id}" in ref for ref in action.exercised_by)
        )

    def test_unreferenced_capability_reports_no_verifying_reference(self) -> None:
        capabilities = self._build(
            (
                {
                    "relationship": "references_route_path",
                    "source_path": "scripts/smoke.py",
                    "target_ref": "/action/attack/",
                },
            )
        )
        player = next(item for item in capabilities if item.label == "player")
        self.assertEqual(player.verification, "no-verifying-reference")
        self.assertEqual(player.exercised_by, ())

    def test_harness_calling_its_own_helper_is_not_coverage(self) -> None:
        capabilities = self._build(
            (
                {
                    "relationship": "calls",
                    "source_path": "scripts/smoke.py",
                    "target_ref": "run",
                },
            )
        )
        scripts = [item for item in capabilities if item.label == "scripts"]
        self.assertTrue(scripts)
        self.assertEqual(scripts[0].verification, "no-verifying-reference")

    def test_call_into_another_file_does_count_as_coverage(self) -> None:
        capabilities = self._build(
            (
                {
                    "relationship": "calls",
                    "source_path": "tests/test_api.py",
                    "target_ref": "helper",
                },
            )
        )
        backend = next(item for item in capabilities if item.kind == "module")
        self.assertEqual(backend.verification, "exercised")

    def test_calls_from_non_verifying_files_are_ignored(self) -> None:
        capabilities = self._build(
            (
                {
                    "relationship": "calls",
                    "source_path": "backend/main.py",
                    "target_ref": "helper",
                },
            )
        )
        backend = next(item for item in capabilities if item.kind == "module")
        self.assertEqual(backend.verification, "no-verifying-reference")

    def test_route_handlers_are_not_duplicated_into_module_clusters(self) -> None:
        capabilities = self._build(())
        module_symbols = {
            symbol for item in capabilities if item.kind == "module" for symbol in item.symbols
        }
        self.assertNotIn("backend.main.attack", module_symbols)
        self.assertIn("backend.util.helper", module_symbols)

    def test_verifying_paths_include_test_roles_and_harness_claims(self) -> None:
        paths = verifying_paths(self.FILES, self.CLAIMS, self.EVIDENCE)
        self.assertEqual(paths, frozenset({"tests/test_api.py", "scripts/smoke.py"}))

    def test_output_is_deterministic(self) -> None:
        first = self._build(())
        second = self._build(())
        self.assertEqual([item.to_dict() for item in first], [item.to_dict() for item in second])


class QualifiedNameTests(TestCase):
    """Name normalization across languages that punctuate differently.

    A Rust crate with 178 passing tests reported no verifying reference for any
    capability. Four things were wrong in sequence, and this was the one that
    made the other three invisible: comparing a full path against a bare name
    cannot succeed, so it failed silently and read as absent verification
    rather than as a name that never normalized.
    """

    def test_a_dotted_name_reduces_to_its_last_segment(self) -> None:
        self.assertEqual(_short_name("app.services.billing.charge"), "charge")

    def test_a_rust_path_reduces_to_its_last_segment(self) -> None:
        self.assertEqual(_short_name("crates::core::compat::check_build"), "check_build")

    def test_a_mixed_separator_name_reduces_to_its_last_segment(self) -> None:
        # Module paths are recorded with dots and Rust items with colons, so a
        # qualified name can carry both.
        self.assertEqual(_short_name("crates.core::compat::check_build"), "check_build")

    def test_a_bare_name_is_returned_unchanged(self) -> None:
        self.assertEqual(_short_name("check_build"), "check_build")
