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


def _occurrences(haystack: str, needle: str, *, whole_word: bool = True) -> list[int]:
    """Where a needle appears, optionally as its own word.

    An answer is bounded. It is a value being asserted, and a raw substring
    search answered "which exception signals a decode failure" with
    `UnicodeDecodeError` and would answer "how many retries" with any number
    containing 10. The boundary is applied only where the needle has one, so a
    pattern or a dotted version keeps its punctuation end open.

    A subject is not bounded. It only anchors where to look, and requiring an
    exact word there made `ascension` fail to find `ascensions` -- scoring a
    claim that held the answer two rows down as unreachable, which sent me
    looking for a reader that was not missing.
    """

    if not whole_word:
        found: list[int] = []
        start = 0
        while True:
            index = haystack.find(needle, start)
            if index < 0:
                return found
            found.append(index)
            start = index + 1

    left = r"(?<!\w)" if needle[:1].isalnum() or needle[:1] == "_" else ""
    right = r"(?!\w)" if needle[-1:].isalnum() or needle[-1:] == "_" else ""
    pattern = re.compile(left + re.escape(needle) + right)
    return [match.start() for match in pattern.finditer(haystack)]


def _score(document: str, question: dict[str, Any]) -> tuple[bool, bool]:
    """Return (answered, cited) for one question against one document."""

    haystack = document.casefold()
    # Every occurrence, not the first. A subject mentioned in a table of contents
    # and again beside its answer would otherwise anchor only to the contents.
    positions: list[int] = []
    for subject in (item.casefold() for item in question["subject"]):
        positions.extend(_occurrences(haystack, subject, whole_word=False))
    if not positions:
        return False, False

    answered = False
    for answer in question["answers"]:
        for found in _occurrences(haystack, answer.casefold()):
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
    # Optional, because a second fixture has no commercial document to compare
    # against. Scoring one document alone still answers the question that
    # matters on an unseen repository -- did the specification answer what a
    # maintainer asked -- and requiring a baseline would have confined this
    # instrument to the single repository a baseline exists for.
    parser.add_argument("--baseline", type=Path)
    # The rendered document is what a person reads and it is deliberately
    # bounded: a panel prints its first rows and says how many more are
    # carried in the exports. The exports are what an agent reads. Scoring
    # only the first understates what the engine delivers, and scoring only
    # the second would hide that a reader cannot see it -- so both are
    # reported, and neither replaces the other.
    parser.add_argument("--structured", type=Path, help="spec.json, scored alongside.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--candidate-label", default="Open Skeleton")
    parser.add_argument("--baseline-label", default="Baseline")
    args = parser.parse_args()

    payload = json.loads(args.questions.read_text(encoding="utf-8"))
    candidate = args.candidate.read_text(encoding="utf-8")
    baseline = args.baseline.read_text(encoding="utf-8") if args.baseline else ""
    structured = args.structured.read_text(encoding="utf-8") if args.structured else ""

    rows: list[dict[str, Any]] = []
    for question in payload["questions"]:
        ours, our_cite = _score(candidate, question)
        theirs, their_cite = _score(baseline, question) if baseline else (False, False)
        exported, _exported_cite = _score(structured, question) if structured else (False, False)
        rows.append(
            {
                "id": question["id"],
                "question": question["question"],
                "kind": question["kind"],
                "candidate_answered": ours,
                "candidate_cited": our_cite,
                "baseline_answered": theirs,
                "baseline_cited": their_cite,
                "structured_answered": exported,
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
        # Without a baseline the comparison columns are dropped rather than
        # filled with dashes, which would read as a baseline that scored zero.
        f"| Measure | {args.candidate_label} | {args.baseline_label} |\n|---|---:|---:|\n"
        if baseline
        else f"| Measure | {args.candidate_label} |\n|---|---:|\n",
        f"| Answered | {ours_answered}/{total} | {theirs_answered}/{total} |\n"
        if baseline
        else f"| Answered | {ours_answered}/{total} |\n",
        f"| Answered with a nearby citation | {ours_cited}/{total} | {theirs_cited}/{total} |\n"
        if baseline
        else f"| Answered with a nearby citation | {ours_cited}/{total} |\n",
        "\n## Per question\n\n",
        f"| # | Question | {args.candidate_label} | {args.baseline_label} |\n|---|---|---|---|\n"
        if baseline
        else f"| # | Question | {args.candidate_label} |\n|---|---|---|\n",
    ]

    def mark(answered: bool, cited: bool) -> str:
        if answered and cited:
            return "answered + cited"
        if answered:
            return "answered"
        return "—"

    for row in rows:
        rendered = (
            f"| {row['id']} | {row['question']} | "
            f"{mark(row['candidate_answered'], row['candidate_cited'])} |"
        )
        if baseline:
            rendered += f" {mark(row['baseline_answered'], row['baseline_cited'])} |"
        lines.append(rendered + "\n")

    if structured:
        exported_total = sum(row["structured_answered"] for row in rows)
        only_exported = [
            row["id"]
            for row in rows
            if row["structured_answered"] and not row["candidate_answered"]
        ]
        lines.append(
            f"\nThe rendered document answered {ours_answered}/{total}. "
            f"The structured export answered {exported_total}/{total}"
        )
        if only_exported:
            lines.append(
                f", carrying {', '.join(only_exported)} that the document bounded "
                "away. A panel prints its first rows and names how many more it "
                "holds, so these are reachable by an agent reading `spec.json` and "
                "not by a reader of the page.\n"
            )
        else:
            lines.append(". Nothing was reachable in one and not the other.\n")

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
