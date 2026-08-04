# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol


SYNTHESIS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "claim_ids": {"type": "array", "items": {"type": "string"}},
                    "narrative": {"type": "string"},
                    "caveats": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["claim_ids", "narrative", "caveats"],
            },
        },
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "findings", "conflicts", "unknowns"],
}


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    task: str
    snapshot_id: str
    context_pack: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=lambda: SYNTHESIS_SCHEMA)
    model: str | None = None
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ValueError("Provider task cannot be empty")
        if not 1 <= self.timeout_seconds <= 3_600:
            raise ValueError("Provider timeout must be between 1 and 3600 seconds")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider: str
    status: str
    snapshot_id: str
    request_sha256: str
    duration_ms: int
    output: dict[str, Any] | None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderAdapter(Protocol):
    name: str

    def generate(self, request: ProviderRequest, *, workspace: Path) -> ProviderResult: ...


def _request_hash(request: ProviderRequest) -> str:
    payload = json.dumps(
        request.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _provider_prompt(request: ProviderRequest) -> str:
    return (
        "You are a synthesis adapter for Open Skeleton. Use only the supplied context pack. "
        "Do not claim you inspected source outside it. Preserve conflict and unknown states. "
        "Every finding must cite claim_ids from the pack. Return only JSON matching the supplied "
        "schema.\n\n"
        f"TASK:\n{request.task}\n\n"
        f"OUTPUT_SCHEMA:\n{json.dumps(request.output_schema, sort_keys=True)}\n\n"
        f"CONTEXT_PACK:\n{json.dumps(request.context_pack, sort_keys=True, ensure_ascii=False)}"
    )


def _parse_structured_output(value: str) -> dict[str, Any]:
    document = json.loads(value)
    if not isinstance(document, dict):
        raise ValueError("Provider output must be a JSON object")
    required = {"summary", "findings", "conflicts", "unknowns"}
    missing = required - document.keys()
    if missing:
        raise ValueError(f"Provider output is missing fields: {', '.join(sorted(missing))}")
    extra = document.keys() - required
    if extra:
        raise ValueError(f"Provider output has unsupported fields: {', '.join(sorted(extra))}")
    if not isinstance(document["summary"], str):
        raise ValueError("Provider summary must be a string")
    for key in ("conflicts", "unknowns"):
        if not isinstance(document[key], list):
            raise ValueError(f"Provider field {key} must be an array")
        if not all(isinstance(item, str) for item in document[key]):
            raise ValueError(f"Provider field {key} must contain only strings")
    if not isinstance(document["findings"], list):
        raise ValueError("Provider field findings must be an array")
    finding_fields = {"claim_ids", "narrative", "caveats"}
    for finding in document["findings"]:
        if not isinstance(finding, dict) or set(finding) != finding_fields:
            raise ValueError("Each provider finding must contain claim_ids, narrative, and caveats")
        if not isinstance(finding["narrative"], str):
            raise ValueError("Provider finding narrative must be a string")
        if not isinstance(finding["claim_ids"], list) or not all(
            isinstance(item, str) for item in finding["claim_ids"]
        ):
            raise ValueError("Provider finding claim_ids must be an array of strings")
        if not isinstance(finding["caveats"], list) or not all(
            isinstance(item, str) for item in finding["caveats"]
        ):
            raise ValueError("Provider finding caveats must be an array of strings")
    return document


def _validate_claim_references(output: dict[str, Any], request: ProviderRequest) -> None:
    available = {
        str(item.get("claim_id"))
        for item in request.context_pack.get("claims", [])
        if isinstance(item, dict) and item.get("claim_id")
    }
    cited = {
        claim_id
        for finding in output["findings"]
        for claim_id in finding["claim_ids"]
    }
    unknown = cited - available
    if unknown:
        raise ValueError(
            "Provider cited claim IDs outside the supplied context pack: "
            + ", ".join(sorted(unknown))
        )


class DisabledProvider:
    name = "disabled"

    def generate(self, request: ProviderRequest, *, workspace: Path) -> ProviderResult:
        del workspace
        return ProviderResult(
            provider=self.name,
            status="disabled",
            snapshot_id=request.snapshot_id,
            request_sha256=_request_hash(request),
            duration_ms=0,
            output=None,
            error="Provider synthesis is disabled; deterministic analysis remains available.",
        )


class LocalCommandProvider:
    """Explicit local adapter: JSON request on stdin, structured JSON on stdout."""

    name = "local-command"

    def __init__(self, command: Sequence[str]) -> None:
        if not command or not all(part for part in command):
            raise ValueError("Local provider command cannot be empty")
        self.command = tuple(command)

    def generate(self, request: ProviderRequest, *, workspace: Path) -> ProviderResult:
        started = time.perf_counter()
        request_hash = _request_hash(request)
        try:
            completed = subprocess.run(
                self.command,
                input=json.dumps(request.to_dict(), ensure_ascii=False),
                text=True,
                capture_output=True,
                check=False,
                cwd=workspace,
                timeout=request.timeout_seconds,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"local provider exited {completed.returncode}: {completed.stderr[-1000:]}"
                )
            output = _parse_structured_output(completed.stdout)
            _validate_claim_references(output, request)
            return ProviderResult(
                provider=self.name,
                status="complete",
                snapshot_id=request.snapshot_id,
                request_sha256=request_hash,
                duration_ms=round((time.perf_counter() - started) * 1000),
                output=output,
                metadata={"command": list(self.command)},
            )
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            return ProviderResult(
                provider=self.name,
                status="error",
                snapshot_id=request.snapshot_id,
                request_sha256=request_hash,
                duration_ms=round((time.perf_counter() - started) * 1000),
                output=None,
                error=f"{exc.__class__.__name__}: {exc}",
                metadata={"command": list(self.command)},
            )


class CodexCliProvider:
    """Opt-in Codex CLI synthesis in an ephemeral, read-only sandbox."""

    name = "codex-cli"

    def __init__(self, executable: str = "codex") -> None:
        self.executable = executable

    def generate(self, request: ProviderRequest, *, workspace: Path) -> ProviderResult:
        started = time.perf_counter()
        request_hash = _request_hash(request)
        workspace = workspace.expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(dir=workspace, prefix="provider-codex-") as temporary:
                temporary_path = Path(temporary)
                schema_path = temporary_path / "schema.json"
                output_path = temporary_path / "output.json"
                schema_path.write_text(
                    json.dumps(request.output_schema, sort_keys=True), encoding="utf-8"
                )
                command = [
                    self.executable,
                    "exec",
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "--cd",
                    str(workspace),
                ]
                if request.model:
                    command.extend(["--model", request.model])
                command.append("-")
                completed = subprocess.run(
                    command,
                    input=_provider_prompt(request),
                    text=True,
                    capture_output=True,
                    check=False,
                    cwd=workspace,
                    timeout=request.timeout_seconds,
                )
                if completed.returncode != 0:
                    raise RuntimeError(
                        f"Codex exited {completed.returncode}: {completed.stderr[-1000:]}"
                    )
                output = _parse_structured_output(output_path.read_text(encoding="utf-8"))
                _validate_claim_references(output, request)
            return ProviderResult(
                provider=self.name,
                status="complete",
                snapshot_id=request.snapshot_id,
                request_sha256=request_hash,
                duration_ms=round((time.perf_counter() - started) * 1000),
                output=output,
                metadata={"model": request.model, "sandbox": "read-only", "ephemeral": True},
            )
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            return ProviderResult(
                provider=self.name,
                status="error",
                snapshot_id=request.snapshot_id,
                request_sha256=request_hash,
                duration_ms=round((time.perf_counter() - started) * 1000),
                output=None,
                error=f"{exc.__class__.__name__}: {exc}",
                metadata={"model": request.model, "sandbox": "read-only", "ephemeral": True},
            )


class ClaudeCliProvider:
    """Opt-in Claude Code print-mode synthesis with mutation tools denied."""

    name = "claude-cli"

    def __init__(self, executable: str = "claude") -> None:
        self.executable = executable

    def generate(self, request: ProviderRequest, *, workspace: Path) -> ProviderResult:
        started = time.perf_counter()
        request_hash = _request_hash(request)
        workspace = workspace.expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        command = [
            self.executable,
            "--print",
            "--output-format",
            "json",
            "--permission-mode",
            "plan",
            "--max-turns",
            "1",
            "--disallowedTools",
            "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Read,Glob,Grep",
        ]
        if request.model:
            command.extend(["--model", request.model])
        try:
            completed = subprocess.run(
                command,
                input=_provider_prompt(request),
                text=True,
                capture_output=True,
                check=False,
                cwd=workspace,
                timeout=request.timeout_seconds,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Claude exited {completed.returncode}: {completed.stderr[-1000:]}"
                )
            envelope = json.loads(completed.stdout)
            if not isinstance(envelope, dict) or envelope.get("is_error"):
                raise ValueError("Claude returned an error or malformed JSON envelope")
            output = _parse_structured_output(str(envelope.get("result", "")))
            _validate_claim_references(output, request)
            return ProviderResult(
                provider=self.name,
                status="complete",
                snapshot_id=request.snapshot_id,
                request_sha256=request_hash,
                duration_ms=round((time.perf_counter() - started) * 1000),
                output=output,
                metadata={
                    "model": request.model,
                    "session_id": envelope.get("session_id"),
                    "total_cost_usd": envelope.get("total_cost_usd"),
                    "permission_mode": "plan",
                },
            )
        except (
            OSError,
            json.JSONDecodeError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
        ) as exc:
            return ProviderResult(
                provider=self.name,
                status="error",
                snapshot_id=request.snapshot_id,
                request_sha256=request_hash,
                duration_ms=round((time.perf_counter() - started) * 1000),
                output=None,
                error=f"{exc.__class__.__name__}: {exc}",
                metadata={"model": request.model, "permission_mode": "plan"},
            )
