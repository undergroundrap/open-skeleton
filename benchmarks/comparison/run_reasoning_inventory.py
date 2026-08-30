# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Build a one-to-one review inventory of an external document's reasoning.

Fact coverage can establish that two artifacts name the same symbols and
quantities. It cannot establish that they reach the same conclusion. This tool
does not disguise lexical similarity as semantic agreement. Instead it:

1. extracts every prose unit carrying explicit causal, consequence, risk, or
   constraint language from a registered baseline;
2. checks whether its code anchors exist in the pinned repository and whether
   they were already present in the ingestion context;
3. retrieves the most closely related candidate unit as an adjudication aid;
4. leaves the conclusion verdict unscored until a reviewer supplies a sidecar.

The output is the finite one-to-one queue a human or bounded review agent can
adjudicate as equivalent, partial, missing, baseline-incorrect, or unjudgeable.
No model is invoked and no private baseline text is redistributed.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from run_comparison import (
    DEFAULT_BASELINE_INVENTORY,
    _load_baseline_record,
    _sha256,
    _verify_baseline_artifact,
    _verify_repository,
)

SCHEMA = "open-skeleton.reasoning-inventory.v1"
ADJUDICATION_SCHEMA = "open-skeleton.reasoning-adjudications.v1"
ADJUDICATION_STATUSES = frozenset(
    {"equivalent", "partial", "missing", "baseline_incorrect", "unjudgeable"}
)
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
CODE_SPAN = re.compile(r"`([^`\n]{2,160})`")
PATH = re.compile(
    r"(?:^|[/\\])?[\w.-]+(?:[/\\][\w.-]+)*\."
    r"(?:py|tsx?|jsx?|css|md|json|toml|rs|go|java|sql|ya?ml|ini|cfg|txt)$",
    re.I,
)
IDENTIFIER = re.compile(r"^[A-Za-z_][\w.]*(?:\(\))?$")
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
LINE_SUFFIX = re.compile(r"(?::\d+(?:-\d+)?|\s+L\d+(?:-\d+)?)$", re.I)
MARKERS: dict[str, int] = {
    "therefore": 5,
    "which means": 5,
    "this means": 5,
    "implies": 5,
    "bottleneck": 5,
    "single point": 5,
    "because": 4,
    "coupled": 4,
    "risk": 4,
    "constraint": 3,
    "without": 2,
    "cannot": 2,
    "failure": 2,
}
STOPWORDS = frozenset(
    {
        "about",
        "against",
        "also",
        "because",
        "been",
        "being",
        "between",
        "cannot",
        "does",
        "every",
        "from",
        "have",
        "into",
        "only",
        "other",
        "should",
        "that",
        "their",
        "there",
        "therefore",
        "these",
        "this",
        "through",
        "under",
        "which",
        "with",
        "without",
        "would",
    }
)


@dataclass(frozen=True, slots=True)
class TextUnit:
    heading: str
    line: int
    text: str


def _markdown_units(document: str, *, include_rows: bool = False) -> list[TextUnit]:
    """Fence-aware prose units with their containing heading and source line."""

    units: list[TextUnit] = []
    heading = ""
    fenced = False
    paragraph: list[str] = []
    paragraph_line = 1

    def flush() -> None:
        nonlocal paragraph
        if paragraph:
            value = " ".join(item.strip() for item in paragraph).strip()
            if value:
                units.append(TextUnit(heading=heading, line=paragraph_line, text=value))
            paragraph = []

    for line_number, line in enumerate((*document.splitlines(), ""), start=1):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        match = HEADING.match(line)
        if match is not None:
            flush()
            heading = match.group(2).strip()
            continue
        row = stripped.startswith(("|", "- "))
        if not line.strip() or row:
            flush()
            if include_rows and row and len(stripped) >= 20:
                units.append(TextUnit(heading=heading, line=line_number, text=stripped))
            continue
        if not paragraph:
            paragraph_line = line_number
        paragraph.append(line)
    return units


def _reasoning_score(text: str) -> tuple[int, list[str]]:
    folded = text.casefold()
    matched = [marker for marker in MARKERS if marker in folded]
    return sum(MARKERS[item] for item in matched), matched


def _anchor(raw: str) -> str | None:
    value = LINE_SUFFIX.sub("", raw.strip()).strip().replace("\\", "/")
    if PATH.fullmatch(value):
        return value.casefold()
    if IDENTIFIER.fullmatch(value):
        normalized = value.rstrip("()").casefold()
        if "." in normalized or len(normalized) >= 6:
            return normalized
    return None


def _anchors(text: str) -> frozenset[str]:
    return frozenset(
        value for raw in CODE_SPAN.findall(text) if (value := _anchor(raw)) is not None
    )


def _tokens(text: str) -> frozenset[str]:
    return frozenset(
        value for token in TOKEN.findall(text) if (value := token.casefold()) not in STOPWORDS
    )


def _anchor_equivalent(left: str, right: str) -> bool:
    if left == right:
        return True
    if "/" in left or "/" in right:
        return left.endswith(f"/{right}") or right.endswith(f"/{left}")
    # A paragraph may name the collection while another names the operation
    # performed on it (`cache` versus `cache.keys`). Both locate the same
    # source object; the prose still requires adjudication after retrieval.
    return left.startswith(f"{right}.") or right.startswith(f"{left}.")


def _anchor_recall(expected: frozenset[str], observed: frozenset[str]) -> float:
    if not expected:
        return 0.0
    hits = sum(
        any(_anchor_equivalent(item, candidate) for candidate in observed) for item in expected
    )
    return hits / len(expected)


def _unit_id(heading: str, line: int, text: str) -> str:
    payload = f"{heading}\n{line}\n{text}".encode()
    return "reasoning-" + hashlib.sha256(payload).hexdigest()[:20]


def _repository_text(root: Path) -> str:
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            parts.append(path.read_text(encoding="utf-8", errors="ignore").casefold())
        except OSError:
            continue
        parts.append(path.relative_to(root).as_posix().casefold())
    return "\n".join(parts)


def _best_candidate(
    baseline_tokens: frozenset[str],
    baseline_anchors: frozenset[str],
    candidates: list[tuple[TextUnit, frozenset[str], frozenset[str]]],
) -> tuple[TextUnit | None, float, float, float]:
    best: TextUnit | None = None
    best_score = 0.0
    best_anchor_recall = 0.0
    best_lexical_recall = 0.0
    for unit, candidate_tokens, candidate_anchors in candidates:
        lexical = (
            len(baseline_tokens & candidate_tokens) / len(baseline_tokens)
            if baseline_tokens
            else 0.0
        )
        anchor_recall = _anchor_recall(baseline_anchors, candidate_anchors)
        score = 0.7 * anchor_recall + 0.3 * lexical if baseline_anchors else lexical
        if score > best_score:
            best = unit
            best_score = score
            best_anchor_recall = anchor_recall
            best_lexical_recall = lexical
    return best, best_score, best_anchor_recall, best_lexical_recall


def _load_adjudications(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != ADJUDICATION_SCHEMA or not isinstance(
        document.get("adjudications"), list
    ):
        raise ValueError("Unsupported reasoning adjudication sidecar")
    result: dict[str, dict[str, Any]] = {}
    for item in document["adjudications"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("Every reasoning adjudication requires an id")
        status = item.get("status")
        if status not in ADJUDICATION_STATUSES:
            raise ValueError(f"Unsupported reasoning adjudication status: {status!r}")
        if item["id"] in result:
            raise ValueError(f"Duplicate reasoning adjudication id: {item['id']}")
        result[item["id"]] = item
    return result


def _render(report: dict[str, Any], sample: int) -> str:
    summary = report["summary"]
    lines = [
        "# Reasoning review inventory\n\n",
        (
            "One row per causal, consequence, risk, or constraint-bearing prose unit in "
            "the registered external baseline. Retrieval is an aid to review, not a "
            "semantic score. A related paragraph can still reach a different conclusion.\n\n"
        ),
        "| Measure | Result |\n|---|---:|\n",
        f"| Baseline reasoning units | {summary['reasoning_units']:,} |\n",
        f"| Units with repository-grounded anchors | {summary['grounded_units']:,} |\n",
        f"| Units carrying prompt-seeded anchors | {summary['prompt_seeded_units']:,} |\n",
        f"| Candidate-related retrievals | {summary['candidate_related']:,} |\n",
        f"| Review-gap retrievals | {summary['review_gap']:,} |\n",
        f"| Adjudicated | {summary['adjudicated']:,} |\n",
        f"| Awaiting adjudication | {summary['unadjudicated']:,} |\n\n",
    ]
    if summary["conclusion_coverage"] is None:
        lines.append(
            "**Conclusion coverage is deliberately not reported.** One or more units "
            "still lack source-aware adjudication; lexical retrieval cannot fill that role.\n\n"
        )
    else:
        lines.append(
            f"Adjudicated exact conclusion coverage: **{summary['conclusion_coverage']:.1%}**. "
            f"At-least-partial coverage: **{summary['at_least_partial_coverage']:.1%}**.\n\n"
        )
    lines.extend(
        [
            "## Highest-priority unadjudicated units\n\n",
            "| ID | Baseline section | Grounding | Prompt hint | Retrieval |\n",
            "|---|---|---|---|---:|\n",
        ]
    )
    pending = [item for item in report["units"] if item["adjudication"] is None]
    pending.sort(
        key=lambda item: (
            item["triage"] != "review_gap",
            -item["reasoning_score"],
            item["id"],
        )
    )
    for item in pending[:sample]:
        heading = str(item["baseline"]["heading"]).replace("|", "\\|")
        lines.append(
            f"| `{item['id']}` | {heading} | {item['grounding']} | "
            f"{item['prompt_hint']} | {item['retrieval']['score']:.1%} |\n"
        )
    lines.append(
        "\nThe JSON artifact contains the complete text, anchors, best candidate unit, "
        "and stable IDs used by an adjudication sidecar.\n"
    )
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--baseline-id", required=True)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--adjudications", type=Path)
    parser.add_argument("--baseline-inventory", type=Path, default=DEFAULT_BASELINE_INVENTORY)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--sample", type=int, default=30)
    args = parser.parse_args()

    record = _load_baseline_record(args.baseline_inventory, args.baseline_id)
    baseline_receipt = _verify_baseline_artifact(args.baseline, record)
    repository_receipt = _verify_repository(args.repo, record)
    baseline_text = args.baseline.read_text(encoding="utf-8")
    candidate_text = args.candidate.read_text(encoding="utf-8")
    repository = _repository_text(args.repo)
    context = args.context.read_text(encoding="utf-8").casefold() if args.context else ""
    context_tokens = _tokens(context)
    adjudications = _load_adjudications(args.adjudications)

    candidate_units = [
        (item, _tokens(item.text), _anchors(item.text))
        for item in _markdown_units(candidate_text, include_rows=True)
    ]
    units: list[dict[str, Any]] = []
    seen_adjudications: set[str] = set()
    for item in _markdown_units(baseline_text):
        reasoning_score, markers = _reasoning_score(item.text)
        if reasoning_score < 5 or not 30 <= len(item.text) <= 1_200:
            continue
        anchors = _anchors(item.text)
        grounded = frozenset(value for value in anchors if value in repository)
        seeded = frozenset(value for value in anchors if value in context)
        baseline_tokens = _tokens(f"{item.heading} {item.text}")
        best, score, anchor_recall, lexical_recall = _best_candidate(
            baseline_tokens, anchors, candidate_units
        )
        unit_id = _unit_id(item.heading, item.line, item.text)
        adjudication = adjudications.get(unit_id)
        if adjudication is not None:
            seen_adjudications.add(unit_id)
        if grounded:
            triage = "candidate_related" if score >= 0.35 and anchor_recall >= 0.5 else "review_gap"
            grounding = "repository-grounded"
        else:
            triage = "unscorable_no_grounded_anchor"
            grounding = "no-grounded-anchor"
        heading_tokens = _tokens(item.heading)
        topic_seed_share = (
            len(heading_tokens & context_tokens) / len(heading_tokens) if heading_tokens else 0.0
        )
        if seeded:
            prompt_hint = "anchor-seeded"
        elif topic_seed_share >= 0.5:
            prompt_hint = "topic-seeded"
        else:
            prompt_hint = "not-seeded"
        units.append(
            {
                "id": unit_id,
                "reasoning_score": reasoning_score,
                "markers": markers,
                "grounding": grounding,
                "prompt_hint": prompt_hint,
                "triage": triage,
                "anchors": sorted(anchors),
                "grounded_anchors": sorted(grounded),
                "prompt_seeded_anchors": sorted(seeded),
                "prompt_topic_seed_share": round(topic_seed_share, 6),
                "baseline": {"heading": item.heading, "line": item.line, "text": item.text},
                "retrieval": {
                    "score": round(score, 6),
                    "anchor_recall": round(anchor_recall, 6),
                    "lexical_recall": round(lexical_recall, 6),
                    "candidate": (
                        {"heading": best.heading, "line": best.line, "text": best.text}
                        if best is not None
                        else None
                    ),
                },
                "adjudication": adjudication,
            }
        )

    unused = sorted(set(adjudications) - seen_adjudications)
    if unused:
        raise ValueError("Adjudication sidecar contains unknown IDs: " + ", ".join(unused))
    status_counts = collections.Counter(
        str(item["adjudication"]["status"]) for item in units if item["adjudication"] is not None
    )
    adjudicated = sum(status_counts.values())
    eligible = len(units)
    complete = adjudicated == eligible and eligible > 0
    summary = {
        "reasoning_units": eligible,
        "grounded_units": sum(item["grounding"] == "repository-grounded" for item in units),
        "prompt_seeded_units": sum(item["prompt_hint"] != "not-seeded" for item in units),
        "candidate_related": sum(item["triage"] == "candidate_related" for item in units),
        "review_gap": sum(item["triage"] == "review_gap" for item in units),
        "adjudicated": adjudicated,
        "unadjudicated": eligible - adjudicated,
        "adjudication_statuses": dict(sorted(status_counts.items())),
        "conclusion_coverage": status_counts["equivalent"] / eligible if complete else None,
        "at_least_partial_coverage": (
            (status_counts["equivalent"] + status_counts["partial"]) / eligible
            if complete
            else None
        ),
    }
    report = {
        "schema": SCHEMA,
        "baseline": {"id": record["id"], **baseline_receipt},
        "repository": repository_receipt,
        "candidate": {
            "path": str(args.candidate.resolve()),
            "bytes": args.candidate.stat().st_size,
            "sha256": _sha256(args.candidate),
        },
        "context": (
            {
                "path": str(args.context.resolve()),
                "bytes": args.context.stat().st_size,
                "sha256": _sha256(args.context),
            }
            if args.context
            else None
        ),
        "summary": summary,
        "units": units,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "reasoning-inventory.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "reasoning-inventory.md").write_text(
        _render(report, args.sample), encoding="utf-8"
    )
    print(_render(report, args.sample), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
