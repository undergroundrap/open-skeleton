# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

import json
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from open_skeleton.providers import (
    REASONING_REVIEW_BATCH_CONTRACT,
    REASONING_REVIEW_BATCH_SCHEMA,
    SYNTHESIS_SCHEMA,
    ClaudeCliProvider,
    CodexCliProvider,
    DisabledProvider,
    LocalCommandProvider,
    ProviderRequest,
)


def _review_request(*, unit_ids: tuple[str, ...] = ("reasoning-one",)) -> ProviderRequest:
    return ProviderRequest(
        task="Review every supplied reasoning unit",
        snapshot_id="snapshot",
        context_pack={
            "review_units": [{"id": unit_id} for unit_id in unit_ids],
            "evidence": [
                {"evidence_id": f"evidence-{index}"} for index, _ in enumerate(unit_ids, start=1)
            ],
            "candidate_units": [{"id": "candidate-one"}],
        },
        output_contract=REASONING_REVIEW_BATCH_CONTRACT,
    )


def _review(
    unit_id: str = "reasoning-one",
    *,
    status: str = "equivalent",
    materiality: str = "material",
    baseline_validity: str = "supported",
    evidence_ids: list[str] | None = None,
    candidate_unit_ids: list[str] | None = None,
    proposal: bool = True,
) -> dict[str, Any]:
    return {
        "id": unit_id,
        "status": status,
        "materiality": materiality,
        "baseline_validity": baseline_validity,
        "proposal": proposal,
        "rationale": "The supplied receipts support this proposed relation.",
        "evidence_ids": evidence_ids if evidence_ids is not None else ["evidence-1"],
        "candidate_unit_ids": (
            candidate_unit_ids if candidate_unit_ids is not None else ["candidate-one"]
        ),
        "caveats": [],
    }


class ProviderTests(TestCase):
    def test_default_request_preserves_synthesis_wire_contract(self) -> None:
        request = ProviderRequest(task="Summarize", snapshot_id="snapshot", context_pack={})

        self.assertEqual(request.output_schema, SYNTHESIS_SCHEMA)
        self.assertNotIn("output_contract", request.to_dict())

    def test_disabled_provider_is_a_first_class_no_model_mode(self) -> None:
        request = ProviderRequest(task="Summarize", snapshot_id="snapshot", context_pack={})
        result = DisabledProvider().generate(request, workspace=Path.cwd())

        self.assertEqual(result.status, "disabled")
        self.assertIsNone(result.output)

    def test_local_command_uses_shared_json_contract(self) -> None:
        response = {
            "summary": "bounded",
            "findings": [],
            "conflicts": [],
            "unknowns": ["runtime behavior"],
        }
        program = f"import json,sys; request=json.load(sys.stdin); print(json.dumps({response!r}))"
        request = ProviderRequest(
            task="Summarize evidence",
            snapshot_id="snapshot",
            context_pack={"claims": []},
        )
        with TemporaryDirectory() as temporary:
            result = LocalCommandProvider([sys.executable, "-c", program]).generate(
                request, workspace=Path(temporary)
            )

        self.assertEqual(result.status, "complete", result.error)
        self.assertEqual(result.output, response)
        self.assertEqual(len(result.request_sha256), 64)

    def test_local_command_rejects_unstructured_output(self) -> None:
        request = ProviderRequest(task="Summarize", snapshot_id="snapshot", context_pack={})
        with TemporaryDirectory() as temporary:
            result = LocalCommandProvider([sys.executable, "-c", "print('not json')"]).generate(
                request, workspace=Path(temporary)
            )

        self.assertEqual(result.status, "error")
        self.assertIn("JSONDecodeError", result.error or "")

    def test_local_command_rejects_unknown_claim_references(self) -> None:
        response = {
            "summary": "bad citation",
            "findings": [{"claim_ids": ["invented"], "narrative": "unsupported", "caveats": []}],
            "conflicts": [],
            "unknowns": [],
        }
        program = f"import json; print(json.dumps({response!r}))"
        request = ProviderRequest(
            task="Summarize",
            snapshot_id="snapshot",
            context_pack={"claims": [{"claim_id": "real"}]},
        )
        with TemporaryDirectory() as temporary:
            result = LocalCommandProvider([sys.executable, "-c", program]).generate(
                request, workspace=Path(temporary)
            )

        self.assertEqual(result.status, "error")
        self.assertIn("outside the supplied context pack", result.error or "")

    def test_local_command_rejects_extra_schema_fields(self) -> None:
        response = {
            "summary": "extra",
            "findings": [],
            "conflicts": [],
            "unknowns": [],
            "surprise": True,
        }
        program = f"import json; print(json.dumps({response!r}))"
        request = ProviderRequest(task="Summarize", snapshot_id="snapshot", context_pack={})
        with TemporaryDirectory() as temporary:
            result = LocalCommandProvider([sys.executable, "-c", program]).generate(
                request, workspace=Path(temporary)
            )

        self.assertEqual(result.status, "error")
        self.assertIn("unsupported fields", result.error or "")

    def test_reasoning_review_batch_is_strict_and_source_bound(self) -> None:
        request = _review_request(unit_ids=("reasoning-one", "reasoning-two", "reasoning-three"))
        response = {
            "reviews": [
                _review(status="candidate_superset"),
                _review(
                    "reasoning-two",
                    status="not_applicable",
                    materiality="presentation_only",
                    evidence_ids=["evidence-2"],
                    candidate_unit_ids=[],
                ),
                _review(
                    "reasoning-three",
                    status="baseline_incorrect",
                    baseline_validity="incorrect",
                    evidence_ids=["evidence-3"],
                    candidate_unit_ids=[],
                ),
            ]
        }
        program = f"import json; print(json.dumps({response!r}))"

        with TemporaryDirectory() as temporary:
            result = LocalCommandProvider([sys.executable, "-c", program]).generate(
                request, workspace=Path(temporary)
            )

        self.assertEqual(request.output_schema, REASONING_REVIEW_BATCH_SCHEMA)
        self.assertEqual(request.to_dict()["output_contract"], REASONING_REVIEW_BATCH_CONTRACT)
        self.assertEqual(result.status, "complete", result.error)
        self.assertEqual(result.output, response)

    def test_reasoning_review_batch_rejects_missing_units(self) -> None:
        request = _review_request(unit_ids=("reasoning-one", "reasoning-two"))
        response = {"reviews": [_review()]}
        program = f"import json; print(json.dumps({response!r}))"

        with TemporaryDirectory() as temporary:
            result = LocalCommandProvider([sys.executable, "-c", program]).generate(
                request, workspace=Path(temporary)
            )

        self.assertEqual(result.status, "error")
        self.assertIn("does not match requested units", result.error or "")

    def test_reasoning_review_rejects_out_of_pack_references(self) -> None:
        cases = (
            (_review(evidence_ids=["invented-evidence"]), "evidence IDs outside"),
            (
                _review(candidate_unit_ids=["invented-candidate"]),
                "candidate unit IDs outside",
            ),
        )
        for review, message in cases:
            with self.subTest(message=message):
                request = _review_request()
                response = {"reviews": [review]}
                program = f"import json; print(json.dumps({response!r}))"
                with TemporaryDirectory() as temporary:
                    result = LocalCommandProvider([sys.executable, "-c", program]).generate(
                        request, workspace=Path(temporary)
                    )
                self.assertEqual(result.status, "error")
                self.assertIn(message, result.error or "")

    def test_reasoning_review_enforces_materiality_relation(self) -> None:
        cases = (
            (
                _review(status="equivalent", materiality="nonmaterial"),
                "requires not_applicable status",
            ),
            (
                _review(status="not_applicable", materiality="material"),
                "cannot be not_applicable",
            ),
        )
        for review, message in cases:
            with self.subTest(message=message):
                request = _review_request()
                response = {"reviews": [review]}
                program = f"import json; print(json.dumps({response!r}))"
                with TemporaryDirectory() as temporary:
                    result = LocalCommandProvider([sys.executable, "-c", program]).generate(
                        request, workspace=Path(temporary)
                    )
                self.assertEqual(result.status, "error")
                self.assertIn(message, result.error or "")

    def test_baseline_incorrect_review_must_remain_a_proposal(self) -> None:
        request = _review_request()
        response = {
            "reviews": [
                _review(
                    status="baseline_incorrect",
                    baseline_validity="incorrect",
                    candidate_unit_ids=[],
                    proposal=False,
                )
            ]
        }
        program = f"import json; print(json.dumps({response!r}))"

        with TemporaryDirectory() as temporary:
            result = LocalCommandProvider([sys.executable, "-c", program]).generate(
                request, workspace=Path(temporary)
            )

        self.assertEqual(result.status, "error")
        self.assertIn("must remain a proposal", result.error or "")

    def test_codex_adapter_is_ephemeral_read_only_and_schema_bounded(self) -> None:
        request = ProviderRequest(
            task="Synthesize",
            snapshot_id="snapshot",
            context_pack={"claims": []},
            model="example-model",
        )
        response = {
            "summary": "bounded",
            "findings": [],
            "conflicts": [],
            "unknowns": [],
        }

        def complete_codex(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
            output_path = Path(command[command.index("--output-last-message") + 1])
            schema_path = Path(command[command.index("--output-schema") + 1])
            output_path.write_text(json.dumps(response), encoding="utf-8")
            self.assertIn("--ephemeral", command)
            self.assertIn("--skip-git-repo-check", command)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--ignore-rules", command)
            self.assertEqual(command[command.index("--color") + 1], "never")
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertIn("--output-schema", command)
            self.assertEqual(
                json.loads(schema_path.read_text(encoding="utf-8")), request.output_schema
            )
            self.assertEqual(command[command.index("--model") + 1], "example-model")
            self.assertIn("Use only the supplied context pack", kwargs["input"])
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            TemporaryDirectory() as temporary,
            patch("open_skeleton.providers.subprocess.run", side_effect=complete_codex),
        ):
            result = CodexCliProvider("codex-test").generate(request, workspace=Path(temporary))

        self.assertEqual(result.status, "complete", result.error)
        self.assertEqual(result.output, response)
        self.assertEqual(result.metadata["sandbox"], "read-only")
        self.assertTrue(result.metadata["ephemeral"])

    def test_claude_adapter_is_restricted_structured_and_nonpersistent(self) -> None:
        request = ProviderRequest(
            task="Synthesize",
            snapshot_id="snapshot",
            context_pack={"claims": []},
        )
        response = {
            "summary": "bounded",
            "findings": [],
            "conflicts": [],
            "unknowns": [],
        }

        def complete_claude(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
            self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
            self.assertEqual(command[command.index("--disallowedTools") + 1], "*")
            self.assertEqual(command[command.index("--tools") + 1], "")
            self.assertIn("--restricted", command)
            self.assertIn("--safe-mode", command)
            self.assertIn("--no-session-persistence", command)
            claude_schema = json.loads(command[command.index("--json-schema") + 1])
            self.assertEqual(claude_schema["$schema"], "http://json-schema.org/draft-07/schema#")
            self.assertEqual(claude_schema["properties"], SYNTHESIS_SCHEMA["properties"])
            self.assertIn("Use only the supplied context pack", kwargs["input"])
            envelope = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": response,
                "session_id": "must-not-be-persisted",
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(envelope), "")

        with (
            TemporaryDirectory() as temporary,
            patch("open_skeleton.providers.subprocess.run", side_effect=complete_claude),
        ):
            result = ClaudeCliProvider("claude-test").generate(request, workspace=Path(temporary))

        self.assertEqual(result.status, "complete", result.error)
        self.assertEqual(result.output, response)
        self.assertEqual(result.metadata["permission_mode"], "plan")
        self.assertTrue(result.metadata["restricted"])
        self.assertTrue(result.metadata["safe_mode"])
        self.assertFalse(result.metadata["session_persistence"])
        self.assertNotIn("session_id", result.metadata)

    def test_claude_adapter_does_not_fallback_to_unstructured_result(self) -> None:
        request = ProviderRequest(
            task="Synthesize",
            snapshot_id="snapshot",
            context_pack={"claims": []},
        )
        response = {
            "summary": "old unstructured envelope",
            "findings": [],
            "conflicts": [],
            "unknowns": [],
        }
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(response),
        }
        with (
            TemporaryDirectory() as temporary,
            patch(
                "open_skeleton.providers.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["claude-test"], 0, json.dumps(envelope), ""
                ),
            ),
        ):
            result = ClaudeCliProvider("claude-test").generate(request, workspace=Path(temporary))

        self.assertEqual(result.status, "error")
        self.assertIn("without a structured_output object", result.error or "")
