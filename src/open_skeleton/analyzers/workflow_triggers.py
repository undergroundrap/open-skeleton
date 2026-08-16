# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""What starts a CI workflow, read from the two keys that decide it.

The engine reported the *absence* of continuous integration and said nothing
whatever about the presence of it. A repository with no workflow got a claim;
a repository with one got silence, which is the wrong way round -- what runs
the suite, and when, is a thing a reader asks about a repository that has CI,
not about one that does not.

This is deliberately not a YAML parser. The distribution declares no runtime
dependencies, so there is none available, and writing one to read two keys
would be a large surface for a small fact. It reads `on:` and `jobs:` by
indentation and understands the three spellings GitHub actually accepts:

    on: push
    on: [push, pull_request]
    on:
      push:
        branches: [main]

Anything it cannot read unambiguously it declines to report. A workflow whose
triggers are not recovered produces no claim rather than a guessed one --
"this runs on push" is worth nothing if it might be wrong, and the reader has
no second source to check it against.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from open_skeleton.ids import stable_id
from open_skeleton.models import (
    AnalysisResult,
    ClaimRecord,
    CoverageRecord,
    EvidenceRecord,
    Snapshot,
    utc_now,
)

ANALYZER_VERSION = "workflow-triggers/v1"

# The events a reader treats differently from the rest: one is a person
# choosing to run it, one is a clock. A suite with neither runs only in
# response to code arriving, which is a fact about how it is used.
MANUAL_EVENTS = frozenset({"workflow_dispatch"})
SCHEDULED_EVENTS = frozenset({"schedule"})

# The key may be quoted. `on` is a YAML 1.1 boolean, so a workflow that wants
# to be unambiguous writes `"on":` -- and a reader that only accepts bare keys
# silently finds no triggers in exactly the files whose authors were careful.
KEY = re.compile(r"^(?P<indent>\s*)[\"']?(?P<key>[A-Za-z_][\w-]*)[\"']?\s*:(?P<rest>.*)$")
INLINE_LIST = re.compile(r"^\[(?P<items>[^\]]*)\]$")
MAX_SOURCE_BYTES = 1_000_000
# Beyond this a sentence stops being a sentence.
MAX_NAMED_EVENTS = 8


def _strip_comment(text: str) -> str:
    """Drop a trailing `#` comment that is not inside a quoted scalar."""

    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index]
    return text


def _block_keys(lines: list[str], start: int, indent: int) -> list[str]:
    """Keys nested directly under a block opened at ``start``.

    Only the first level matters here: `push` is a trigger and the `branches`
    beneath it is a filter on that trigger, not another trigger.
    """

    found: list[str] = []
    inner: int | None = None
    for line in lines[start + 1 :]:
        body = _strip_comment(line)
        if not body.strip():
            continue
        match = KEY.match(body)
        if match is None:
            if len(body) - len(body.lstrip()) <= indent:
                break
            continue
        depth = len(match.group("indent"))
        if depth <= indent:
            break
        if inner is None:
            inner = depth
        if depth == inner:
            found.append(match.group("key"))
    return found


def read_workflow(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The triggering events and the job names a workflow declares.

    Returns empty tuples when the file cannot be read confidently, which the
    caller reports as nothing rather than as an absence.
    """

    lines = text.splitlines()
    events: list[str] = []
    jobs: list[str] = []
    for index, line in enumerate(lines):
        body = _strip_comment(line)
        match = KEY.match(body)
        if match is None or match.group("indent"):
            continue
        key = match.group("key")
        # `on` is a YAML 1.1 boolean, which is why some workflows quote it.
        if key not in {"on", "true", "jobs"}:
            continue
        rest = match.group("rest").strip()
        target = jobs if key == "jobs" else events
        if rest:
            listed = INLINE_LIST.match(rest)
            if listed:
                target.extend(
                    item.strip().strip("\"'") for item in listed.group("items").split(",")
                )
            elif key != "jobs":
                target.append(rest.strip("\"'"))
            continue
        target.extend(_block_keys(lines, index, len(match.group("indent"))))
    return tuple(dict.fromkeys(item for item in events if item)), tuple(
        dict.fromkeys(item for item in jobs if item)
    )


class WorkflowTriggerAnalyzer:
    """What starts each CI workflow, and how many jobs it starts."""

    name = "workflow-triggers"
    version = ANALYZER_VERSION
    eligibility = "subject"

    def analyze(self, snapshot: Snapshot) -> AnalysisResult:
        started = time.perf_counter()
        created_at = utc_now()
        evidence: list[EvidenceRecord] = []
        claims: list[ClaimRecord] = []
        failures: list[str] = []

        candidates = [
            item
            for item in snapshot.files
            if str(item.role) == "workflow" and item.size_bytes <= MAX_SOURCE_BYTES
        ]
        eligible: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
        for file_record in candidates:
            source_path = snapshot.root / Path(file_record.path)
            try:
                payload = source_path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != file_record.sha256:
                    raise ValueError("content changed after snapshot")
                source = payload.decode("utf-8", errors="strict")
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                failures.append(f"{file_record.path}: {exc.__class__.__name__}: {exc}")
                continue
            events, jobs = read_workflow(source)
            if events:
                eligible.append((file_record.path, events, jobs))

        for path, events, jobs in eligible:
            record = EvidenceRecord(
                evidence_id=stable_id(
                    "evidence",
                    (snapshot.snapshot_id, path, 1, "workflow_trigger", ANALYZER_VERSION),
                ),
                snapshot_id=snapshot.snapshot_id,
                path=path,
                start_line=1,
                end_line=1,
                symbol=None,
                evidence_kind="workflow_trigger",
                excerpt_sha256=hashlib.sha256(path.encode("utf-8")).hexdigest(),
                analyzer=ANALYZER_VERSION,
                created_at=created_at,
            )
            evidence.append(record)
            named = ", ".join(f"`{item}`" for item in events[:MAX_NAMED_EVENTS])
            text = f"`{path}` runs on {named}"
            if jobs:
                text += f", across {len(jobs):,} job(s)"
            text += "."
            if not (set(events) & (MANUAL_EVENTS | SCHEDULED_EVENTS)):
                # Neither a clock nor a person can start it, so it runs only
                # when code arrives. That bounds when its results can exist.
                text += (
                    " It declares no scheduled or manually dispatched trigger, so it runs "
                    "only in response to code arriving."
                )
            claims.append(
                ClaimRecord(
                    claim_id=stable_id(
                        "claim",
                        (snapshot.snapshot_id, "delivery_automation", text, ANALYZER_VERSION),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    claim=text,
                    category="delivery_automation",
                    status="verified",
                    confidence=1.0,
                    importance="medium",
                    produced_by=ANALYZER_VERSION,
                    created_at=created_at,
                    verified_at=created_at,
                    supporting_evidence=(record.evidence_id,),
                    invalidation_keys=(f"file:{path}",),
                    alternative_hypotheses=(
                        (
                            "This is the workflow as declared. Whether it ran, passed, or is "
                            "enabled on the hosting side is not visible from the repository."
                        ),
                    ),
                )
            )

        coverage = CoverageRecord(
            analyzer=ANALYZER_VERSION,
            language="CI workflows",
            eligible_files=len(eligible),
            analyzed_files=len(eligible),
            failed_files=len(failures),
            unsupported_files=0,
            failures=tuple(sorted(failures)),
        )
        return AnalysisResult(
            snapshot_id=snapshot.snapshot_id,
            analyzer_version=ANALYZER_VERSION,
            created_at=created_at,
            duration_ms=round((time.perf_counter() - started) * 1000),
            symbols=(),
            edges=(),
            evidence=tuple(evidence),
            claims=tuple(claims),
            coverage=(coverage,),
        )
