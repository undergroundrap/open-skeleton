# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Numbers a repository's own documentation records, and the ones it aims at.

A project that measures itself writes the result down, and that writing is the
only record of it that survives. Nothing here re-ran a benchmark or timed
anything; these are quotations with citations, and every claim says so.

The distinction this module exists to hold is between a figure observed and a
figure wanted. Both appear as a number and a unit in a documentation file, and
conflating them is the failure that matters:

    p95 lookup: under 200 ns          <- a budget. Nobody said it was met.
    the probe completed in 2,893 ms   <- an observation.

Reported as one family, the first becomes evidence the system is fast, which
is a claim nobody in the repository made. So they are separate categories with
separate wording, and a line carrying markers of both is dropped rather than
guessed at.

Two deliberate blind spots, both preferring silence to invention:

* **A number with no framing is skipped.** `| check | 0 | 308 ms |` states a
  measurement to a human and nothing at all to this reader, unless the table
  is introduced by a line that frames it -- which is handled, because that is
  how measurement tables are actually written.
* **Fenced code is never read.** A timeout constant in an example is not a
  measurement of anything.
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
    FileRecord,
    Snapshot,
    utc_now,
)

ANALYZER_VERSION = "documented-measurements/v1"

DOCUMENTATION_SUFFIXES = frozenset({".md", ".rst", ".txt"})

# Units specific enough that a bare number carrying one is about magnitude.
# A lone "s" is excluded: it matches too much prose to mean anything.
UNIT = (
    r"(?:ms|µs|us|ns|MB/s|GB/s|KB/s|TB|GB|MB|KB|req/s|ops/s|qps|rps|"
    r"seconds?|minutes?|hours?|%)"
)
QUANTITY = re.compile(rf"\b\d[\d,]*(?:\.\d+)?\s*{UNIT}\b")

# A figure the repository wants. A bound word only counts when a number follows
# it: "queue under a distance-ordered backlog" is a preposition, and reading it
# as a budget turned an entire measured timing table into a table of targets.
# Percentile labels are deliberately absent -- p99 names which figure is being
# reported, not whether it was observed or hoped for.
BUDGET_MARKERS = re.compile(
    r"\b(?:under|within|below|at most|no more than|less than|not exceed(?:ing)?|up to)\s+"
    r"(?:about\s+|around\s+|~\s*)?\d"
    r"|\b(?:budget|target|ceiling|sla|quota)\b"
    r"|[<≤]\s*\d",
    re.IGNORECASE,
)

# A figure the repository saw. Past tense doing the work it does in English,
# plus the summary statistics that only describe a sample already taken.
MEASUREMENT_MARKERS = re.compile(
    r"\b(?:completed in|finished in|took|ran in|runs in|measured|observed|recorded|"
    r"elapsed|benchmark(?:ed|s)?|reproduced|timed at|throughput of|results?)\b",
    re.IGNORECASE,
)

FENCE = re.compile(r"^\s*(?:```|~~~)")
TABLE_ROW = re.compile(r"^\s*\|")
TABLE_RULE = re.compile(r"^\s*\|[\s|:-]+\|?\s*$")

MAX_QUOTED = 150
# A long results table would otherwise contribute a hundred near-identical
# claims and crowd out every other fact in its section. The remainder is
# stated rather than dropped silently.
MAX_LINES_PER_FILE = 20
MAX_SOURCE_BYTES = 2_000_000


def _quote(line: str) -> str:
    """One documentation line, trimmed to something a table cell can hold."""

    collapsed = " ".join(line.strip().strip("|").split())
    if len(collapsed) <= MAX_QUOTED:
        return collapsed
    return collapsed[: MAX_QUOTED - 1].rstrip() + "…"


def classify(line: str, inherited: str | None = None) -> str | None:
    """``"measured"``, ``"budget"``, or ``None`` when the line does not say.

    ``inherited`` carries the framing of the sentence introducing a table, so
    that rows of a results table are read the way a person reads them. A row
    with its own markers overrides what it inherited, because a budget column
    inside a results table is still a budget.
    """

    if not QUANTITY.search(line):
        return None
    budget = bool(BUDGET_MARKERS.search(line))
    measured = bool(MEASUREMENT_MARKERS.search(line))
    if budget and measured:
        # Both readings are available and nothing decides between them.
        return None
    if budget:
        return "budget"
    if measured:
        return "measured"
    return inherited


def scan(text: str) -> list[tuple[int, str, str]]:
    """Every classified line as ``(line number, kind, text)``.

    Fenced blocks are skipped entirely, and a table inherits the framing of the
    last prose line above it.
    """

    found: list[tuple[int, str, str]] = []
    fenced = False
    framing: str | None = None
    in_table = False
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if TABLE_ROW.match(line):
            if TABLE_RULE.match(line):
                continue
            kind = classify(line, framing)
            if kind:
                found.append((number, kind, line))
            in_table = True
            continue
        if in_table:
            in_table = False
            framing = None
        stripped = line.strip()
        if not stripped:
            continue
        kind = classify(line)
        if kind:
            found.append((number, kind, line))
        # A line need not itself carry a quantity to frame the table under it,
        # but it must be introducing something. Requiring the colon is what
        # separates "The probe completed in 2,893 ms:" from a paragraph that
        # merely mentions timing somewhere above an unrelated table.
        # Only a measurement frames a table, and only from a sentence that
        # expresses no want at all. "...under a 1.2 ms budget — under a third
        # of a 240 Hz frame. Individual costs:" introduces measured rows while
        # stating a target, and conferring its framing relabelled every one of
        # them as a target. A budget is never inherited: targets are written
        # per line in practice ("small file `hum check`: under 50 ms"), so
        # inheriting one buys nothing and risks exactly that mislabelling.
        if stripped.endswith(":") and not BUDGET_MARKERS.search(line):
            framing = "measured" if MEASUREMENT_MARKERS.search(line) else None
        else:
            framing = None
    return found


class DocumentedMeasurementAnalyzer:
    """Performance figures a repository states, separated by what they are."""

    name = "documented-measurements"
    version = "v1"

    def analyze(self, snapshot: Snapshot) -> AnalysisResult:
        started = time.perf_counter()
        created_at = utc_now()
        evidence: list[EvidenceRecord] = []
        claims: list[ClaimRecord] = []
        failures: list[str] = []

        eligible = [
            item
            for item in snapshot.files
            if Path(item.path).suffix.lower() in DOCUMENTATION_SUFFIXES
            # Role, not just extension: a markdown fixture under `tests/` is
            # classified `test`, and a number inside one is a value the suite
            # exercises rather than a figure this repository publishes.
            and str(item.role) == "documentation"
            and item.size_bytes <= MAX_SOURCE_BYTES
        ]
        analyzed_files = 0

        for file_record in eligible:
            source_path = snapshot.root / Path(file_record.path)
            try:
                payload = source_path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != file_record.sha256:
                    raise ValueError("content changed after snapshot")
                source = payload.decode("utf-8", errors="strict")
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                failures.append(f"{file_record.path}: {exc.__class__.__name__}: {exc}")
                continue
            analyzed_files += 1
            for kind in ("measured", "budget"):
                lines = [item for item in scan(source) if item[1] == kind]
                if not lines:
                    continue
                self._record(
                    snapshot,
                    created_at,
                    file_record,
                    kind,
                    lines,
                    evidence,
                    claims,
                )

        coverage = CoverageRecord(
            analyzer=ANALYZER_VERSION,
            language="Documentation",
            eligible_files=len(eligible),
            analyzed_files=analyzed_files,
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

    def _record(
        self,
        snapshot: Snapshot,
        created_at: str,
        file_record: FileRecord,
        kind: str,
        lines: list[tuple[int, str, str]],
        evidence: list[EvidenceRecord],
        claims: list[ClaimRecord],
    ) -> None:
        path = file_record.path
        category = "recorded_measurement" if kind == "measured" else "stated_budget"
        verb = "records having measured" if kind == "measured" else "states as a target"
        for number, _, text in lines[:MAX_LINES_PER_FILE]:
            record = EvidenceRecord(
                evidence_id=stable_id(
                    "evidence", (snapshot.snapshot_id, path, number, category, ANALYZER_VERSION)
                ),
                snapshot_id=snapshot.snapshot_id,
                path=path,
                start_line=number,
                end_line=number,
                symbol=None,
                evidence_kind=category,
                excerpt_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                analyzer=ANALYZER_VERSION,
                created_at=created_at,
            )
            evidence.append(record)
            claims.append(
                self._claim(
                    snapshot,
                    created_at,
                    text=f"`{path}` {verb}: {_quote(text)}",
                    category=category,
                    supporting=(record.evidence_id,),
                    path=path,
                )
            )
        withheld = len(lines) - MAX_LINES_PER_FILE
        if withheld > 0:
            # Stated rather than dropped: a reader who sees twenty rows of a
            # forty-row results table must not conclude there were twenty.
            claims.append(
                self._claim(
                    snapshot,
                    created_at,
                    text=(
                        f"`{path}` carries {len(lines):,} line(s) this reader classified as "
                        f"{'measurements' if kind == 'measured' else 'stated targets'}; "
                        f"{withheld:,} beyond the first {MAX_LINES_PER_FILE} are not listed "
                        "individually."
                    ),
                    category=category,
                    supporting=(evidence[-1].evidence_id,),
                    path=path,
                )
            )

    def _claim(
        self,
        snapshot: Snapshot,
        created_at: str,
        *,
        text: str,
        category: str,
        supporting: tuple[str, ...],
        path: str,
    ) -> ClaimRecord:
        return ClaimRecord(
            claim_id=stable_id("claim", (snapshot.snapshot_id, category, text, ANALYZER_VERSION)),
            snapshot_id=snapshot.snapshot_id,
            claim=text,
            category=category,
            status="verified",
            confidence=1.0,
            importance="medium",
            produced_by=ANALYZER_VERSION,
            created_at=created_at,
            verified_at=created_at,
            supporting_evidence=supporting,
            invalidation_keys=(f"file:{path}",),
            alternative_hypotheses=(
                (
                    "What is verified is that the document says this, on this line. "
                    "Nothing here re-ran the measurement, checked the hardware it was "
                    "taken on, or established that it still holds."
                ),
            ),
        )
