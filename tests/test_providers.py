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
    ClaudeCliProvider,
    CodexCliProvider,
    DisabledProvider,
    LocalCommandProvider,
    ProviderRequest,
)


class ProviderTests(TestCase):
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
            output_path.write_text(json.dumps(response), encoding="utf-8")
            self.assertIn("--ephemeral", command)
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertIn("--output-schema", command)
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

    def test_claude_adapter_denies_repository_and_mutation_tools(self) -> None:
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
            denied = command[command.index("--disallowedTools") + 1].split(",")
            self.assertTrue(
                {"Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebFetch"}.issubset(denied)
            )
            self.assertIn("Use only the supplied context pack", kwargs["input"])
            envelope = {"is_error": False, "result": json.dumps(response)}
            return subprocess.CompletedProcess(command, 0, json.dumps(envelope), "")

        with (
            TemporaryDirectory() as temporary,
            patch("open_skeleton.providers.subprocess.run", side_effect=complete_claude),
        ):
            result = ClaudeCliProvider("claude-test").generate(request, workspace=Path(temporary))

        self.assertEqual(result.status, "complete", result.error)
        self.assertEqual(result.output, response)
        self.assertEqual(result.metadata["permission_mode"], "plan")
