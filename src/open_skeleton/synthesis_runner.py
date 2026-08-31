# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Safe, resumable execution of deterministic synthesis plans."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from open_skeleton.providers import ProviderAdapter, ProviderRequest
from open_skeleton.synthesis_plan import SYNTHESIS_PLAN_SCHEMA

SYNTHESIS_RUN_SCHEMA = "open-skeleton.synthesis_run.v1"
SYNTHESIS_JOB_RESULT_SCHEMA = "open-skeleton.synthesis_job_result.v1"
_SUPPORTED_PROVIDERS = frozenset({"codex-cli", "claude-cli", "local-command"})
_MAX_PLAN_BYTES = 50_000_000
_MAX_CONTEXT_BYTES = 1_000_000
_MAX_CONCURRENCY = 16


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def request_sha256(request: ProviderRequest) -> str:
    """Return the exact canonical hash used by the existing provider adapters."""

    payload = json.dumps(
        request.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return _sha256_bytes(payload)


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
    if len(jobs) > max_jobs:
        raise ValueError(f"Synthesis plan has {len(jobs)} jobs; limit is {max_jobs}")

    seen: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("Every synthesis job must be an object")
        job_id = job.get("job_id")
        task = job.get("task")
        context = job.get("context_pack")
        if not isinstance(job_id, str) or not job_id or job_id in seen:
            raise ValueError("Synthesis job IDs must be nonempty and unique")
        seen.add(job_id)
        if not isinstance(task, str) or not task.strip():
            raise ValueError(f"Synthesis job {job_id!r} has no task")
        if job.get("parallel_safe") is not True:
            raise ValueError(f"Synthesis job {job_id!r} is not marked parallel_safe")
        if not isinstance(context, dict) or context.get("snapshot_id") != snapshot_id:
            raise ValueError(f"Synthesis job {job_id!r} has a mismatched context snapshot")
        claims = context.get("claims")
        max_chars = context.get("max_chars")
        if not isinstance(claims, list) or len(claims) > 100:
            raise ValueError(f"Synthesis job {job_id!r} exceeds the 100-claim limit")
        if not isinstance(max_chars, int) or not 1_000 <= max_chars <= 200_000:
            raise ValueError(f"Synthesis job {job_id!r} has an invalid context bound")
        context_bytes = len(
            json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        )
        if context_bytes > _MAX_CONTEXT_BYTES:
            raise ValueError(f"Synthesis job {job_id!r} exceeds the 1 MB packet limit")
    return document, _sha256_bytes(payload)


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _git_worktree_ancestor(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        marker = candidate / ".git"
        if marker.exists() or marker.is_symlink():
            return candidate
    return None


def validate_external_output(output_dir: Path, source_root: Path) -> Path:
    """Keep source-derived plans and results outside repositories and Git."""

    source = source_root.expanduser().resolve(strict=True)
    output = output_dir.expanduser().resolve()
    if _inside(output, source):
        raise ValueError("Synthesis run output must be outside the analyzed repository")
    worktree = _git_worktree_ancestor(output)
    if worktree is not None:
        raise ValueError(f"Synthesis run output must be outside Git worktrees: {worktree}")
    return output


def _validate_local_command(adapter: ProviderAdapter, source_root: Path) -> None:
    command = getattr(adapter, "command", ())
    if not isinstance(command, tuple):
        return
    source = source_root.expanduser().resolve(strict=True)
    for argument in command:
        candidate = Path(argument).expanduser()
        if not candidate.is_absolute():
            continue
        resolved = candidate.resolve()
        if _inside(resolved, source):
            raise ValueError("Local provider command cannot execute a path from the analyzed repo")


def _safe_name(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.")[:48]
    return slug or "job"


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            json.dump(document, temporary, indent=2, sort_keys=True, ensure_ascii=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary_name).replace(path)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _completed_result(
    path: Path,
    *,
    plan_hash: str,
    job_id: str,
    provider: str,
    request_hash: str,
) -> bool:
    if not path.exists():
        return False
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("schema") != SYNTHESIS_JOB_RESULT_SCHEMA
        or document.get("sensitivity") != "private-source-derived"
        or document.get("plan_sha256") != plan_hash
        or document.get("job_id") != job_id
        or document.get("provider") != provider
        or document.get("request_sha256") != request_hash
    ):
        raise ValueError(f"Existing synthesis result does not match its request: {path}")
    result = document.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"Existing synthesis result has no provider result: {path}")
    if result.get("request_sha256") != request_hash or result.get("provider") != provider:
        raise ValueError(f"Existing synthesis result has a mismatched provider receipt: {path}")
    return result.get("status") == "complete"


def run_synthesis_plan(
    plan_path: Path,
    *,
    source_root: Path,
    output_dir: Path,
    adapter: ProviderAdapter,
    execute: bool = False,
    model: str | None = None,
    timeout_seconds: int = 300,
    concurrency: int = 1,
    max_jobs: int = 100,
) -> dict[str, Any]:
    """Plan or execute independent provider jobs without touching target code."""

    if not 1 <= concurrency <= _MAX_CONCURRENCY:
        raise ValueError(f"Concurrency must be between 1 and {_MAX_CONCURRENCY}")
    if not 1 <= max_jobs <= 1_000:
        raise ValueError("max_jobs must be between 1 and 1000")
    if adapter.name not in _SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported synthesis provider: {adapter.name}")
    source = source_root.expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ValueError(f"Analyzed repository is not a directory: {source}")
    output = validate_external_output(output_dir, source)
    _validate_local_command(adapter, source)
    plan, plan_hash = _load_plan(plan_path, max_jobs=max_jobs)
    snapshot_id = str(plan["snapshot_id"])

    prepared: list[dict[str, Any]] = []
    for index, job in enumerate(plan["jobs"]):
        request = ProviderRequest(
            task=str(job["task"]),
            snapshot_id=snapshot_id,
            context_pack=job["context_pack"],
            model=model,
            timeout_seconds=timeout_seconds,
        )
        request_hash = request_sha256(request)
        job_token = _sha256_bytes(str(job["job_id"]).encode("utf-8"))[:16]
        artifact = (
            output
            / "jobs"
            / (
                f"{_safe_name(adapter.name)}-{_safe_name(str(job['job_id']))}-"
                f"{job_token}-{plan_hash[:16]}-{request_hash[:24]}.json"
            )
        )
        resumed = _completed_result(
            artifact,
            plan_hash=plan_hash,
            job_id=str(job["job_id"]),
            provider=adapter.name,
            request_hash=request_hash,
        )
        prepared.append(
            {
                "index": index,
                "job_id": str(job["job_id"]),
                "request": request,
                "request_sha256": request_hash,
                "artifact": artifact,
                "resumed": resumed,
            }
        )

    records: list[dict[str, Any]] = []
    pending = [item for item in prepared if not item["resumed"]]
    for item in prepared:
        if item["resumed"]:
            records.append(
                {
                    "index": item["index"],
                    "job_id": item["job_id"],
                    "request_sha256": item["request_sha256"],
                    "status": "resumed",
                    "artifact": str(item["artifact"]),
                }
            )
        elif not execute:
            records.append(
                {
                    "index": item["index"],
                    "job_id": item["job_id"],
                    "request_sha256": item["request_sha256"],
                    "status": "planned",
                    "artifact": str(item["artifact"]),
                }
            )

    def invoke(item: dict[str, Any]) -> dict[str, Any]:
        workspace = output / "workspaces" / str(item["request_sha256"])
        workspace.mkdir(parents=True, exist_ok=True)
        result = adapter.generate(item["request"], workspace=workspace)
        if result.request_sha256 != item["request_sha256"]:
            raise ValueError(f"Provider returned the wrong request hash for {item['job_id']}")
        envelope = {
            "schema": SYNTHESIS_JOB_RESULT_SCHEMA,
            "sensitivity": "private-source-derived",
            "plan_sha256": plan_hash,
            "job_id": item["job_id"],
            "provider": adapter.name,
            "request_sha256": item["request_sha256"],
            "result": result.to_dict(),
        }
        _atomic_json(item["artifact"], envelope)
        return {
            "index": item["index"],
            "job_id": item["job_id"],
            "request_sha256": item["request_sha256"],
            "status": result.status,
            "artifact": str(item["artifact"]),
        }

    if execute and pending:
        output.mkdir(parents=True, exist_ok=True)
        output = validate_external_output(output, source)
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(invoke, item): item for item in pending}
            for future in as_completed(futures):
                records.append(future.result())

    records.sort(key=lambda item: int(item["index"]))
    for item in records:
        item.pop("index", None)
    counts: dict[str, int] = {}
    for item in records:
        status = str(item["status"])
        counts[status] = counts.get(status, 0) + 1
    return {
        "schema": SYNTHESIS_RUN_SCHEMA,
        "sensitivity": "private-source-derived",
        "plan_sha256": plan_hash,
        "snapshot_id": snapshot_id,
        "provider": adapter.name,
        "execute": execute,
        "concurrency": concurrency,
        "job_count": len(records),
        "status_counts": dict(sorted(counts.items())),
        "output_dir": str(output),
        "jobs": records,
    }
