# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Score two specifications on questions a maintainer actually asks.

Word count and diagram count measure the shape of a document. They do not
measure whether it answers anything. This scores both documents against a
question set whose ground truth came from the source, not from either document.

A document scores an **answer** when the expected token appears near the
question's subject. It scores a **citation** only when a file reference sits
close to that answer, because an answer a reader cannot locate is worth less
than one they can.

The scoring is deliberately generous to prose: any occurrence within the window
counts, so a long document is not penalised for burying the answer.

    python benchmarks/comparison/run_questions.py \\
        --questions benchmarks/comparison/questions.json \\
        --candidate <spec.md> --baseline <tech_spec.md> \\
        --output-dir <dir>
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

WINDOW = 400
FILE_REFERENCE = re.compile(
    r"[\w./*-]+\.(?:py|tsx|ts|js|jsx|css|md|json|toml|rs|ya?ml|txt|lock|cfg|ini)"
)


def _score(document: str, question: dict[str, Any]) -> tuple[bool, bool]:
    """Return (answered, cited) for one question against one document."""

    haystack = document.casefold()
    # Every occurrence, not the first. A subject mentioned in a table of contents
    # and again beside its answer would otherwise anchor only to the contents.
    positions: list[int] = []
    for subject in (item.casefold() for item in question["subject"]):
        start = 0
        while True:
            found = haystack.find(subject, start)
            if found < 0:
                break
            positions.append(found)
            start = found + 1
    if not positions:
        return False, False

    answered = False
    for answer in question["answers"]:
        needle = answer.casefold()
        start = 0
        while True:
            found = haystack.find(needle, start)
            if found < 0:
                break
            start = found + 1
            # The answer must sit near a mention of what was asked about,
            # otherwise a stray number anywhere in the document would count.
            if not any(abs(found - anchor) <= WINDOW for anchor in positions):
                continue
            answered = True
            window = document[max(0, found - WINDOW) : found + WINDOW]
            if FILE_REFERENCE.search(window):
                # Any cited occurrence settles it; an earlier uncited mention of
                # the same answer must not mask a later cited one.
                return True, True
    return answered, False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--candidate-label", default="Open Skeleton")
    parser.add_argument("--baseline-label", default="Baseline")
    args = parser.parse_args()

    payload = json.loads(args.questions.read_text(encoding="utf-8"))
    candidate = args.candidate.read_text(encoding="utf-8")
    baseline = args.baseline.read_text(encoding="utf-8")

    rows: list[dict[str, Any]] = []
    for question in payload["questions"]:
        ours, our_cite = _score(candidate, question)
        theirs, their_cite = _score(baseline, question)
        rows.append(
            {
                "id": question["id"],
                "question": question["question"],
                "kind": question["kind"],
                "candidate_answered": ours,
                "candidate_cited": our_cite,
                "baseline_answered": theirs,
                "baseline_cited": their_cite,
            }
        )

    total = len(rows)
    ours_answered = sum(row["candidate_answered"] for row in rows)
    ours_cited = sum(row["candidate_cited"] for row in rows)
    theirs_answered = sum(row["baseline_answered"] for row in rows)
    theirs_cited = sum(row["baseline_cited"] for row in rows)

    lines = [
        "# Question benchmark\n\n",
        (
            f"{total} questions a maintainer asks before changing this system. "
            "Ground truth came from the source, not from either document.\n\n"
        ),
        f"| Measure | {args.candidate_label} | {args.baseline_label} |\n|---|---:|---:|\n",
        f"| Answered | {ours_answered}/{total} | {theirs_answered}/{total} |\n",
        f"| Answered with a nearby citation | {ours_cited}/{total} | {theirs_cited}/{total} |\n",
        "\n## Per question\n\n",
        f"| # | Question | {args.candidate_label} | {args.baseline_label} |\n|---|---|---|---|\n",
    ]

    def mark(answered: bool, cited: bool) -> str:
        if answered and cited:
            return "answered + cited"
        if answered:
            return "answered"
        return "—"

    for row in rows:
        lines.append(
            f"| {row['id']} | {row['question']} | "
            f"{mark(row['candidate_answered'], row['candidate_cited'])} | "
            f"{mark(row['baseline_answered'], row['baseline_cited'])} |\n"
        )

    missed = [row["id"] for row in rows if not row["candidate_answered"]]
    if missed:
        lines.append(
            f"\n**{args.candidate_label} did not answer:** {', '.join(missed)}. "
            "These are the gaps worth closing.\n"
        )
    lines.append(
        "\n## Method and its limits\n\n"
        "An answer counts when the expected token appears within "
        f"{WINDOW} characters of a mention of the question's subject. That is "
        "generous to long documents: burying the answer costs nothing. It is a "
        "keyword test, so it cannot judge whether surrounding prose explains the "
        "answer well, and a document could match a token while saying something "
        "wrong around it. Treat the answered column as an upper bound for both "
        "documents.\n"
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = "".join(lines)
    (args.output_dir / "questions.md").write_text(report, encoding="utf-8", newline="\n")
    (args.output_dir / "questions.json").write_text(
        json.dumps({"rows": rows}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
