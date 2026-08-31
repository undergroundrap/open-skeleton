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

SYNTHESIS_CONTRACT = "open-skeleton.synthesis.v1"
REASONING_REVIEW_BATCH_CONTRACT = "open-skeleton.reasoning-review-batch.v1"
SUPPORTED_OUTPUT_CONTRACTS = frozenset({SYNTHESIS_CONTRACT, REASONING_REVIEW_BATCH_CONTRACT})

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

REASONING_REVIEW_BATCH_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "status": {
                        "type": "string",
                        "enum": [
                            "equivalent",
                            "candidate_superset",
                            "partial",
                            "missing",
                            "contradictory",
                            "baseline_incorrect",
                            "unjudgeable",
                            "not_applicable",
                        ],
                    },
                    "materiality": {
                        "type": "string",
                        "enum": [
                            "material",
                            "nonmaterial",
                            "duplicate",
                            "code",
                            "presentation_only",
                            "unjudgeable",
                        ],
                    },
                    "baseline_validity": {
                        "type": "string",
                        "enum": ["supported", "incorrect", "unjudgeable"],
                    },
                    "proposal": {"type": "boolean", "const": True},
                    "rationale": {"type": "string", "minLength": 1},
                    "evidence_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "candidate_unit_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "caveats": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "id",
                    "status",
                    "materiality",
                    "baseline_validity",
                    "proposal",
                    "rationale",
                    "evidence_ids",
                    "candidate_unit_ids",
                    "caveats",
                ],
            },
        }
    },
    "required": ["reviews"],
}

REASONING_REVIEW_STATUSES = frozenset(
    {
        "equivalent",
        "candidate_superset",
        "partial",
        "missing",
        "contradictory",
        "baseline_incorrect",
        "unjudgeable",
        "not_applicable",
    }
)
REASONING_REVIEW_MATERIALITY = frozenset(
    {"material", "nonmaterial", "duplicate", "code", "presentation_only", "unjudgeable"}
)
REASONING_REVIEW_BASELINE_VALIDITY = frozenset({"supported", "incorrect", "unjudgeable"})
NONMATERIAL_REVIEW_KINDS = frozenset({"nonmaterial", "duplicate", "code", "presentation_only"})


def _schema_for_contract(contract: str) -> dict[str, Any]:
    if contract == SYNTHESIS_CONTRACT:
        return SYNTHESIS_SCHEMA
    if contract == REASONING_REVIEW_BATCH_CONTRACT:
        return REASONING_REVIEW_BATCH_SCHEMA
    raise ValueError(f"Unsupported provider output contract: {contract!r}")


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    task: str
    snapshot_id: str
    context_pack: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    model: str | None = None
    timeout_seconds: int = 300
    output_contract: str = SYNTHESIS_CONTRACT

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ValueError("Provider task cannot be empty")
        if not 1 <= self.timeout_seconds <= 3_600:
            raise ValueError("Provider timeout must be between 1 and 3600 seconds")
        if self.output_contract not in SUPPORTED_OUTPUT_CONTRACTS:
            raise ValueError(f"Unsupported provider output contract: {self.output_contract!r}")
        if self.output_schema is None:
            object.__setattr__(
                self,
                "output_schema",
                _schema_for_contract(self.output_contract),
            )
        elif self.output_schema != _schema_for_contract(self.output_contract):
            raise ValueError("Provider output schema does not match its declared contract")
        if self.output_contract == REASONING_REVIEW_BATCH_CONTRACT:
            _review_unit_ids(self.context_pack)
            _available_ids(self.context_pack, "evidence", "evidence_id")
            _available_ids(self.context_pack, "candidate_units", "id")

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        if self.output_contract == SYNTHESIS_CONTRACT:
            # Preserve the original synthesis request wire shape for existing
            # local-command adapters. Reasoning review consumers need the
            # explicit discriminator because their output shape is different.
            document.pop("output_contract")
        return document


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
    contract_instruction = (
        "Preserve conflict and unknown states. Every finding must cite claim_ids from the pack."
        if request.output_contract == SYNTHESIS_CONTRACT
        else (
            "Return exactly one review for every supplied review unit. Use only supplied evidence "
            "and candidate unit IDs. Treat every review as a proposal. A definitive status requires "
            "source evidence; candidate comparisons also require at least one candidate unit ID."
        )
    )
    return (
        "You are a bounded provider adapter for Open Skeleton. Use only the supplied context pack. "
        "Do not claim you inspected source outside it. "
        f"{contract_instruction} Return only JSON matching the supplied schema.\n\n"
        f"TASK:\n{request.task}\n\n"
        f"OUTPUT_CONTRACT:\n{request.output_contract}\n\n"
        f"OUTPUT_SCHEMA:\n{json.dumps(request.output_schema, sort_keys=True)}\n\n"
        f"CONTEXT_PACK:\n{json.dumps(request.context_pack, sort_keys=True, ensure_ascii=False)}"
    )


def _claude_output_schema(request: ProviderRequest) -> dict[str, Any]:
    """Return the same output shape using the draft supported by Claude Code."""

    schema = dict(request.output_schema or {})
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"
    return schema


def _review_unit_ids(context_pack: dict[str, Any]) -> tuple[str, ...]:
    units = context_pack.get("review_units")
    if not isinstance(units, list) or not units:
        raise ValueError("Reasoning-review context requires a non-empty review_units array")
    unit_ids: list[str] = []
    for unit in units:
        if not isinstance(unit, dict) or not isinstance(unit.get("id"), str):
            raise ValueError("Every reasoning-review unit requires a string id")
        unit_id = unit["id"].strip()
        if not unit_id:
            raise ValueError("Every reasoning-review unit requires a non-empty id")
        unit_ids.append(unit_id)
    if len(unit_ids) != len(set(unit_ids)):
        raise ValueError("Reasoning-review unit IDs must be unique")
    return tuple(unit_ids)


def _available_ids(context_pack: dict[str, Any], key: str, id_key: str) -> set[str]:
    values = context_pack.get(key, [])
    if not isinstance(values, list):
        raise ValueError(f"Reasoning-review context field {key} must be an array")
    identifiers: list[str] = []
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get(id_key), str):
            raise ValueError(
                f"Every reasoning-review context item in {key} requires a string {id_key}"
            )
        identifier = item[id_key].strip()
        if not identifier:
            raise ValueError(
                f"Every reasoning-review context item in {key} requires a non-empty {id_key}"
            )
        identifiers.append(identifier)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"Reasoning-review context IDs in {key} must be unique")
    return set(identifiers)


def _parse_json_object(value: str) -> dict[str, Any]:
    document = json.loads(value)
    if not isinstance(document, dict):
        raise ValueError("Provider output must be a JSON object")
    return document


def _validate_synthesis_output(document: dict[str, Any]) -> None:
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


def _validate_claim_references(output: dict[str, Any], request: ProviderRequest) -> None:
    available = {
        str(item.get("claim_id"))
        for item in request.context_pack.get("claims", [])
        if isinstance(item, dict) and item.get("claim_id")
    }
    cited = {claim_id for finding in output["findings"] for claim_id in finding["claim_ids"]}
    unknown = cited - available
    if unknown:
        raise ValueError(
            "Provider cited claim IDs outside the supplied context pack: "
            + ", ".join(sorted(unknown))
        )


def _string_array(value: Any, *, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Reasoning-review field {field_name} must be an array of strings")
    if any(not item.strip() for item in value):
        raise ValueError(f"Reasoning-review field {field_name} cannot contain empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"Reasoning-review field {field_name} must contain unique IDs")
    return value


def _validate_reasoning_review_output(document: dict[str, Any], request: ProviderRequest) -> None:
    if set(document) != {"reviews"} or not isinstance(document["reviews"], list):
        raise ValueError("Reasoning-review output must contain only a reviews array")
    expected_ids = _review_unit_ids(request.context_pack)
    available_evidence = _available_ids(request.context_pack, "evidence", "evidence_id")
    available_candidates = _available_ids(request.context_pack, "candidate_units", "id")
    required_fields = {
        "id",
        "status",
        "materiality",
        "baseline_validity",
        "proposal",
        "rationale",
        "evidence_ids",
        "candidate_unit_ids",
        "caveats",
    }
    seen_ids: list[str] = []
    for review in document["reviews"]:
        if not isinstance(review, dict) or set(review) != required_fields:
            raise ValueError(
                "Each reasoning review must contain id, status, rationale, evidence_ids, "
                "candidate_unit_ids, and caveats"
            )
        unit_id = review["id"]
        status = review["status"]
        materiality = review["materiality"]
        baseline_validity = review["baseline_validity"]
        rationale = review["rationale"]
        if not isinstance(unit_id, str) or not unit_id.strip():
            raise ValueError("Every reasoning review requires a non-empty string id")
        if not isinstance(status, str) or status not in REASONING_REVIEW_STATUSES:
            raise ValueError(f"Unsupported reasoning-review status: {status!r}")
        if not isinstance(materiality, str) or materiality not in REASONING_REVIEW_MATERIALITY:
            raise ValueError(f"Unsupported reasoning-review materiality: {materiality!r}")
        if (
            not isinstance(baseline_validity, str)
            or baseline_validity not in REASONING_REVIEW_BASELINE_VALIDITY
        ):
            raise ValueError(
                f"Unsupported reasoning-review baseline validity: {baseline_validity!r}"
            )
        if review["proposal"] is not True:
            raise ValueError("Every reasoning review must remain a proposal")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("Every reasoning review requires a non-empty rationale")
        evidence_ids = _string_array(review["evidence_ids"], field_name="evidence_ids")
        candidate_ids = _string_array(review["candidate_unit_ids"], field_name="candidate_unit_ids")
        _string_array(review["caveats"], field_name="caveats")
        unknown_evidence = set(evidence_ids) - available_evidence
        if unknown_evidence:
            raise ValueError(
                "Reasoning review cited evidence IDs outside the supplied context pack: "
                + ", ".join(sorted(unknown_evidence))
            )
        unknown_candidates = set(candidate_ids) - available_candidates
        if unknown_candidates:
            raise ValueError(
                "Reasoning review cited candidate unit IDs outside the supplied context pack: "
                + ", ".join(sorted(unknown_candidates))
            )
        if status != "unjudgeable" and not evidence_ids:
            raise ValueError(f"Reasoning-review status {status!r} requires source evidence")
        if (
            status
            in {
                "equivalent",
                "candidate_superset",
                "partial",
                "contradictory",
            }
            and not candidate_ids
        ):
            raise ValueError(f"Reasoning-review status {status!r} requires a candidate unit")
        if materiality in NONMATERIAL_REVIEW_KINDS and status != "not_applicable":
            raise ValueError(
                f"Reasoning-review materiality {materiality!r} requires not_applicable status"
            )
        if materiality == "material" and status == "not_applicable":
            raise ValueError("Material reasoning-review units cannot be not_applicable")
        if status == "baseline_incorrect" and baseline_validity != "incorrect":
            raise ValueError("A baseline_incorrect proposal requires baseline_validity incorrect")
        seen_ids.append(unit_id)
    if len(seen_ids) != len(set(seen_ids)):
        raise ValueError("Reasoning-review output contains duplicate review IDs")
    missing = set(expected_ids) - set(seen_ids)
    unknown = set(seen_ids) - set(expected_ids)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing: " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown: " + ", ".join(sorted(unknown)))
        raise ValueError(
            "Reasoning-review batch does not match requested units (" + "; ".join(details) + ")"
        )


def _validate_provider_output(document: dict[str, Any], request: ProviderRequest) -> dict[str, Any]:
    if request.output_contract == SYNTHESIS_CONTRACT:
        _validate_synthesis_output(document)
        _validate_claim_references(document, request)
    elif request.output_contract == REASONING_REVIEW_BATCH_CONTRACT:
        _validate_reasoning_review_output(document, request)
    else:  # Defensive: ProviderRequest rejects this before an adapter can run.
        raise ValueError(f"Unsupported provider output contract: {request.output_contract!r}")
    return document


def _parse_structured_output(value: str, request: ProviderRequest) -> dict[str, Any]:
    return _validate_provider_output(_parse_json_object(value), request)


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
            error="Provider invocation is disabled; deterministic analysis remains available.",
            metadata={"output_contract": request.output_contract},
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
            # The command is the user's explicit provider choice, never
            # repository content, and runs without a shell.
            completed = subprocess.run(  # noqa: S603
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
            output = _parse_structured_output(completed.stdout, request)
            return ProviderResult(
                provider=self.name,
                status="complete",
                snapshot_id=request.snapshot_id,
                request_sha256=request_hash,
                duration_ms=round((time.perf_counter() - started) * 1000),
                output=output,
                metadata={
                    "command": list(self.command),
                    "output_contract": request.output_contract,
                },
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
                metadata={
                    "command": list(self.command),
                    "output_contract": request.output_contract,
                },
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
                    "--skip-git-repo-check",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--color",
                    "never",
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
                # Explicit provider choice; no shell, fixed argument vector.
                completed = subprocess.run(  # noqa: S603
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
                output = _parse_structured_output(output_path.read_text(encoding="utf-8"), request)
            return ProviderResult(
                provider=self.name,
                status="complete",
                snapshot_id=request.snapshot_id,
                request_sha256=request_hash,
                duration_ms=round((time.perf_counter() - started) * 1000),
                output=output,
                metadata={
                    "model": request.model,
                    "sandbox": "read-only",
                    "ephemeral": True,
                    "output_contract": request.output_contract,
                },
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
                metadata={
                    "model": request.model,
                    "sandbox": "read-only",
                    "ephemeral": True,
                    "output_contract": request.output_contract,
                },
            )


class ClaudeCliProvider:
    """Opt-in Claude Code structured output with no tools or saved session."""

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
            "--json-schema",
            json.dumps(_claude_output_schema(request), sort_keys=True, separators=(",", ":")),
            "--restricted",
            "--safe-mode",
            "--no-session-persistence",
            "--permission-mode",
            "plan",
            "--max-turns",
            "1",
            "--tools",
            "",
            "--disallowedTools",
            "*",
        ]
        if request.model:
            command.extend(["--model", request.model])
        try:
            # The command is the user's explicit provider choice, never
            # repository content, and runs without a shell.
            completed = subprocess.run(  # noqa: S603
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
            if not isinstance(envelope, dict):
                raise ValueError("Claude returned an error or malformed JSON envelope")
            if envelope.get("is_error") is True or envelope.get("subtype") != "success":
                subtype = envelope.get("subtype", "unknown")
                raise ValueError(f"Claude structured output failed with subtype {subtype!r}")
            structured_output = envelope.get("structured_output")
            if not isinstance(structured_output, dict):
                raise ValueError("Claude reported success without a structured_output object")
            output = _validate_provider_output(structured_output, request)
            return ProviderResult(
                provider=self.name,
                status="complete",
                snapshot_id=request.snapshot_id,
                request_sha256=request_hash,
                duration_ms=round((time.perf_counter() - started) * 1000),
                output=output,
                metadata={
                    "model": request.model,
                    "total_cost_usd": envelope.get("total_cost_usd"),
                    "permission_mode": "plan",
                    "restricted": True,
                    "safe_mode": True,
                    "session_persistence": False,
                    "structured_output": True,
                    "output_contract": request.output_contract,
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
                metadata={
                    "model": request.model,
                    "permission_mode": "plan",
                    "restricted": True,
                    "safe_mode": True,
                    "session_persistence": False,
                    "structured_output": True,
                    "output_contract": request.output_contract,
                },
            )
