# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from typing import Any
from unittest import TestCase

from open_skeleton.spec.capabilities import Capability
from open_skeleton.spec.consequences import Consequence
from open_skeleton.spec.dossiers import Dossier, build_dossiers, render_dossiers


def _capability(**overrides: Any) -> Capability:
    defaults: dict[str, Any] = {
        "capability_id": "C-001",
        "label": "action",
        "kind": "route-group",
        "routes": ("POST /action/attack",),
        "symbols": ("backend.main.attack",),
        "paths": ("backend/main.py",),
        "claim_ids": ("route-1",),
        "evidence_ids": ("e-route",),
        "exercised_by": ("scripts/smoke.py requests /action/attack",),
    }
    defaults.update(overrides)
    return Capability(**defaults)


CLAIMS: tuple[dict[str, Any], ...] = (
    {
        "claim_id": "route-1",
        "category": "http_route",
        "claim": "POST /action/attack is handled by backend.main.attack.",
        "status": "verified",
        "supporting_evidence": ["e-route"],
    },
    {
        "claim_id": "state-1",
        "category": "process_local_state",
        "claim": "backend.main._attack_times is module-owned.",
        "status": "inferred",
        "supporting_evidence": ["e-state"],
    },
    {
        "claim_id": "other-1",
        "category": "security_boundary",
        "claim": "backend.main configures a wildcard origin.",
        "status": "verified",
        "supporting_evidence": ["e-cors"],
    },
    {
        "claim_id": "elsewhere",
        "category": "storage",
        "claim": "A different file opens SQLite.",
        "status": "verified",
        "supporting_evidence": ["e-other"],
    },
)

EVIDENCE = {
    "e-route": {"path": "backend/main.py", "start_line": 158},
    "e-state": {"path": "backend/main.py", "start_line": 27},
    "e-cors": {"path": "backend/main.py", "start_line": 110},
    "e-other": {"path": "backend/db.py", "start_line": 5},
}


class DossierTests(TestCase):
    def _build(self, **kwargs: Any) -> tuple[Dossier, ...]:
        return build_dossiers((_capability(),), CLAIMS, EVIDENCE, **kwargs)

    def test_claims_touching_the_capability_files_are_grouped(self) -> None:
        dossier = self._build()[0]
        statements = {claim for claim, _, _ in dossier.findings}
        self.assertIn("backend.main configures a wildcard origin.", statements)

    def test_claims_from_other_files_are_excluded(self) -> None:
        dossier = self._build()[0]
        statements = {claim for claim, _, _ in dossier.findings}
        self.assertNotIn("A different file opens SQLite.", statements)

    def test_state_claims_are_separated_from_other_findings(self) -> None:
        dossier = self._build()[0]
        self.assertEqual(len(dossier.touches_state), 1)
        self.assertNotIn(
            "backend.main._attack_times is module-owned.",
            {claim for claim, _, _ in dossier.findings},
        )

    def test_the_capabilitys_own_claims_are_not_repeated_as_findings(self) -> None:
        # The route claim is already the capability's surface.
        dossier = self._build()[0]
        self.assertNotIn(
            "POST /action/attack is handled by backend.main.attack.",
            {claim for claim, _, _ in dossier.findings},
        )

    def test_every_finding_carries_a_location(self) -> None:
        dossier = self._build()[0]
        for _, _, where in dossier.findings:
            self.assertNotEqual(where, "—")

    def test_a_consequence_touching_a_related_claim_is_attached(self) -> None:
        consequence = Consequence(
            rule_id="r",
            statement="Any origin reaching the port reaches every route.",
            severity="critical",
            claim_ids=("other-1",),
        )
        dossier = self._build(consequences=(consequence,))[0]
        self.assertIn("Any origin reaching the port reaches every route.", dossier.consequences)

    def test_a_capability_with_no_files_is_skipped(self) -> None:
        self.assertEqual(build_dossiers((_capability(paths=()),), CLAIMS, EVIDENCE), ())

    def test_render_states_the_absence_of_verification_explicitly(self) -> None:
        dossiers = build_dossiers((_capability(exercised_by=()),), CLAIMS, EVIDENCE)
        rendered = "".join(render_dossiers(dossiers))
        self.assertIn("no test-role file or operator harness reaches", rendered)

    def test_render_is_empty_with_a_stated_reason(self) -> None:
        rendered = "".join(render_dossiers(()))
        self.assertIn("nothing to assemble", rendered)

    def test_output_is_deterministic(self) -> None:
        first = [item.to_dict() for item in self._build()]
        second = [item.to_dict() for item in self._build()]
        self.assertEqual(first, second)
