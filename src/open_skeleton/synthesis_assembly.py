# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Validate completed synthesis receipts and render a separate narrative projection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from open_skeleton.synthesis_plan import SYNTHESIS_PLAN_SCHEMA
from open_skeleton.synthesis_runner import (
    SYNTHESIS_JOB_RESULT_SCHEMA,
    validate_external_output,
)

SYNTHESIS_ASSEMBLY_SCHEMA = "open-skeleton.synthesis_assembly.v1"
_MAX_PLAN_BYTES = 50_000_000
_MAX_RECEIPT_BYTES = 10_000_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_plan(path: Path, *, max_jobs: int) -> tuple[dict[str, Any], str]:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"Synthesis plan is not a file: {resolved}")
    payload = resolved.read_bytes()
    if len(payload) > _MAX_PLAN_BYTES:
        raise ValueError("Synthesis plan exceeds the 50 MB safety limit")
    document = json.loads(payload)
    if not isinstance(document, dict) or document.get("schema") != SYNTHESIS_PLAN_SCHEMA:
        raise ValueError("Unsupported synthesis plan schema")
    snapshot_id = document.get("snapshot_id")
    jobs = document.get("jobs")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("Synthesis plan requires a snapshot_id")
    if not isinstance(jobs, list) or document.get("job_count") != len(jobs):
        raise ValueError("Synthesis plan job_count does not match jobs")
    if not 1 <= len(jobs) <= max_jobs:
        raise ValueError(f"Synthesis plan must contain between 1 and {max_jobs} jobs")

    seen: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("Every synthesis job must be an object")
        job_id = job.get("job_id")
        context = job.get("context_pack")
        if not isinstance(job_id, str) or not job_id or job_id in seen:
            raise ValueError("Synthesis job IDs must be nonempty and unique")
        seen.add(job_id)
        if not isinstance(context, dict) or context.get("snapshot_id") != snapshot_id:
            raise ValueError(f"Synthesis job {job_id!r} has a mismatched context snapshot")
        claims = context.get("claims")
        obligation = context.get("obligation")
        if not isinstance(claims, list):
            raise ValueError(f"Synthesis job {job_id!r} has no claims array")
        if not isinstance(obligation, dict):
            raise ValueError(f"Synthesis job {job_id!r} has no obligation")
        if obligation.get("section_id") != job_id:
            raise ValueError(f"Synthesis job {job_id!r} has a mismatched obligation")
        for field in ("number", "title"):
            value = obligation.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Synthesis job {job_id!r} has no obligation {field}")
    return document, _sha256(payload)


def _string_array(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Provider output field {field} must be an array of strings")
    return value


def _validated_output(output: Any, job: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise ValueError(f"Synthesis job {job['job_id']!r} has no structured output")
    required = {"summary", "findings", "conflicts", "unknowns"}
    if set(output) != required:
        raise ValueError(
            f"Synthesis job {job['job_id']!r} output must contain exactly "
            "summary, findings, conflicts, and unknowns"
        )
    if not isinstance(output["summary"], str):
        raise ValueError("Provider output summary must be a string")
    conflicts = _string_array(output["conflicts"], field="conflicts")
    unknowns = _string_array(output["unknowns"], field="unknowns")
    findings = output["findings"]
    if not isinstance(findings, list):
        raise ValueError("Provider output findings must be an array")

    available_claims = {
        str(item["claim_id"])
        for item in job["context_pack"]["claims"]
        if isinstance(item, dict) and isinstance(item.get("claim_id"), str)
    }
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {
            "claim_ids",
            "narrative",
            "caveats",
        }:
            raise ValueError(
                "Every provider finding must contain claim_ids, narrative, and caveats"
            )
        claim_ids = _string_array(finding["claim_ids"], field="claim_ids")
        if not claim_ids:
            raise ValueError("Every provider finding must cite at least one claim ID")
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("Provider finding claim IDs must be unique")
        unknown_claims = set(claim_ids) - available_claims
        if unknown_claims:
            raise ValueError(
                "Provider finding cited claim IDs outside its frozen job: "
                + ", ".join(sorted(unknown_claims))
            )
        if not isinstance(finding["narrative"], str):
            raise ValueError("Provider finding narrative must be a string")
        _string_array(finding["caveats"], field="caveats")
    return {
        "summary": output["summary"],
        "findings": findings,
        "conflicts": conflicts,
        "unknowns": unknowns,
    }


def _load_receipt(
    path: Path,
    *,
    plan_hash: str,
    snapshot_id: str,
    jobs: dict[str, dict[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Synthesis receipt must be a regular file: {path}")
    payload = path.read_bytes()
    if len(payload) > _MAX_RECEIPT_BYTES:
        raise ValueError(f"Synthesis receipt exceeds the 10 MB safety limit: {path}")
    document = json.loads(payload)
    envelope_fields = {
        "schema",
        "sensitivity",
        "plan_sha256",
        "job_id",
        "provider",
        "request_sha256",
        "result",
    }
    if not isinstance(document, dict) or set(document) != envelope_fields:
        raise ValueError(f"Malformed synthesis receipt envelope: {path}")
    if document.get("schema") != SYNTHESIS_JOB_RESULT_SCHEMA:
        raise ValueError(f"Unsupported synthesis receipt schema: {path}")
    if document.get("sensitivity") != "private-source-derived":
        raise ValueError(f"Synthesis receipt lost its sensitivity label: {path}")
    if document.get("plan_sha256") != plan_hash:
        raise ValueError(f"Synthesis receipt does not match the exact frozen plan: {path}")

    job_id = document.get("job_id")
    provider = document.get("provider")
    request_hash = document.get("request_sha256")
    if not isinstance(job_id, str) or job_id not in jobs:
        raise ValueError(f"Synthesis receipt names an unknown job: {job_id!r}")
    if not isinstance(provider, str) or not provider:
        raise ValueError(f"Synthesis receipt has no provider: {path}")
    if not isinstance(request_hash, str) or _SHA256.fullmatch(request_hash) is None:
        raise ValueError(f"Synthesis receipt has an invalid request hash: {path}")

    result = document["result"]
    result_fields = {
        "provider",
        "status",
        "snapshot_id",
        "request_sha256",
        "duration_ms",
        "output",
        "error",
        "metadata",
    }
    if not isinstance(result, dict) or set(result) != result_fields:
        raise ValueError(f"Malformed provider result in synthesis receipt: {path}")
    if (
        result.get("provider") != provider
        or result.get("snapshot_id") != snapshot_id
        or result.get("request_sha256") != request_hash
    ):
        raise ValueError(f"Synthesis receipt has a mismatched provider result: {path}")
    if result.get("status") != "complete" or result.get("error") is not None:
        raise ValueError(f"Synthesis receipt is not a completed provider result: {path}")
    if not isinstance(result.get("duration_ms"), int) or result["duration_ms"] < 0:
        raise ValueError(f"Synthesis receipt has an invalid duration: {path}")
    if not isinstance(result.get("metadata"), dict):
        raise ValueError(f"Synthesis receipt has invalid provider metadata: {path}")
    return job_id, provider, _validated_output(result.get("output"), jobs[job_id])


def _bullet(value: str) -> str:
    return "- " + value.replace("\n", "\n  ") + "\n"


def _inline_code(value: str) -> str:
    longest = max((len(item) for item in re.findall(r"`+", value)), default=0)
    fence = "`" * (longest + 1)
    padding = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{padding}{value}{padding}{fence}"


def _render(plan: dict[str, Any], plan_hash: str, outputs: dict[str, dict[str, Any]]) -> str:
    lines = [
        "# Source-Grounded Narrative Specification\n\n",
        (
            "> This is a separate narrative projection of a frozen evidence plan. "
            "It does not replace the deterministic specification or evidence ledger.\n\n"
        ),
        f"- Snapshot: {_inline_code(str(plan['snapshot_id']))}\n",
        f"- Profile: {_inline_code(str(plan.get('profile_id', 'unknown')))}\n",
        f"- Plan SHA-256: {_inline_code(plan_hash)}\n\n",
    ]
    for job in plan["jobs"]:
        obligation = job["context_pack"]["obligation"]
        output = outputs[str(job["job_id"])]
        number = str(obligation["number"]).replace("\n", " ").strip()
        title = str(obligation["title"]).replace("\n", " ").strip()
        lines.extend([f"## {number} {title}\n\n", "### Summary\n\n", output["summary"], "\n\n"])
        lines.append("### Findings\n\n")
        if output["findings"]:
            for index, finding in enumerate(output["findings"], start=1):
                lines.extend([f"#### Finding {index}\n\n", finding["narrative"], "\n\n"])
                lines.append("Claim IDs:\n\n")
                lines.extend(_bullet(_inline_code(item)) for item in finding["claim_ids"])
                lines.append("\nCaveats:\n\n")
                if finding["caveats"]:
                    lines.extend(_bullet(item) for item in finding["caveats"])
                else:
                    lines.append("None reported.\n")
                lines.append("\n")
        else:
            lines.append("No findings were returned.\n\n")
        for heading, key in (("Conflicts", "conflicts"), ("Unknowns", "unknowns")):
            lines.append(f"### {heading}\n\n")
            if output[key]:
                lines.extend(_bullet(item) for item in output[key])
            else:
                lines.append("None reported.\n")
            lines.append("\n")
    return "".join(lines)


def _atomic_markdown(path: Path, markdown: str) -> None:
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(markdown)
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary_name).replace(path)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def assemble_synthesis(
    plan_path: Path,
    *,
    results_dir: Path,
    source_root: Path,
    output_path: Path,
    max_jobs: int = 1_000,
) -> dict[str, Any]:
    """Validate one complete result per frozen job and write Markdown atomically."""

    if not 1 <= max_jobs <= 1_000:
        raise ValueError("max_jobs must be between 1 and 1000")
    source = source_root.expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ValueError(f"Analyzed repository is not a directory: {source}")
    output = validate_external_output(output_path, source)
    if output.name.casefold() == "spec.md":
        raise ValueError("Synthesis assembly cannot overwrite deterministic spec.md")
    if output.exists() and output.is_dir():
        raise ValueError(f"Synthesis assembly output must be a file: {output}")

    plan, plan_hash = _load_plan(plan_path, max_jobs=max_jobs)
    jobs = {str(item["job_id"]): item for item in plan["jobs"]}
    results = results_dir.expanduser().resolve(strict=True)
    jobs_dir = results / "jobs"
    if not results.is_dir() or jobs_dir.is_symlink() or not jobs_dir.is_dir():
        raise ValueError("Synthesis results directory must contain a regular jobs directory")
    receipt_paths = sorted(jobs_dir.glob("*.json"))
    if not receipt_paths:
        raise ValueError("Synthesis results directory contains no job receipts")

    outputs: dict[str, dict[str, Any]] = {}
    providers: Counter[str] = Counter()
    for receipt_path in receipt_paths:
        job_id, provider, provider_output = _load_receipt(
            receipt_path,
            plan_hash=plan_hash,
            snapshot_id=str(plan["snapshot_id"]),
            jobs=jobs,
        )
        if job_id in outputs:
            raise ValueError(f"Duplicate synthesis receipt for job {job_id!r}")
        outputs[job_id] = provider_output
        providers[provider] += 1
    missing = set(jobs) - set(outputs)
    if missing:
        raise ValueError("Missing synthesis receipts for jobs: " + ", ".join(sorted(missing)))
    if len(outputs) != len(jobs):
        raise ValueError("Synthesis receipt coverage does not exactly match the frozen plan")

    markdown = _render(plan, plan_hash, outputs)
    output.parent.mkdir(parents=True, exist_ok=True)
    output = validate_external_output(output, source)
    _atomic_markdown(output, markdown)
    return {
        "schema": SYNTHESIS_ASSEMBLY_SCHEMA,
        "sensitivity": "private-source-derived",
        "contacts_model": False,
        "plan_sha256": plan_hash,
        "snapshot_id": str(plan["snapshot_id"]),
        "job_count": len(jobs),
        "provider_counts": dict(sorted(providers.items())),
        "markdown_sha256": _sha256(markdown.encode("utf-8")),
        "artifact": str(output),
    }
