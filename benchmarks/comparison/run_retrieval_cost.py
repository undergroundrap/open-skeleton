# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""What an answer costs an agent that queries instead of reading.

`run_questions.py` asks whether a specification contains an answer. Across
three fixtures the structured export contains all fifty, which retires that
question and raises the one the product actually rests on: an agent does not
read a 1.3 MB export any more than it reads the repository. It queries. So
this measures the query.

For each question the query is built from the question's own subject terms and
never from its answer -- the subject is what an agent extracts from what it was
asked, and using the answer would measure nothing but this script. The result
is scored the same way `run_questions.py` scores a document, and its size is
recorded, because an answer that costs as much as reading the source has saved
nobody anything.

Three costs are reported against each other:

  query      what the retrieval returned
  document   `spec.md`, the whole page
  source     every source file in the repository, which is the upper bound on
             reading it and not what a careful agent would actually spend --
             it greps. Treat the ratio as generous rather than exact.

    python benchmarks/comparison/run_retrieval_cost.py \\
        --questions benchmarks/comparison/questions-urllib3.json \\
        --state-dir <dir> --root <analysed repository>

Exit status is zero. This measures a cost; it is not a gate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.ledger import EvidenceLedger

WINDOW = 400
QUERY_LIMIT = 10
# Used only to ask whether a missed answer was in the index at all.
DEEP_LIMIT = 200
# Characters per token, roughly, for English prose and identifiers. Used only
# to put the byte counts in a unit the reader is paying in; every conclusion
# here holds on the raw bytes too.
CHARACTERS_PER_TOKEN = 4


def _answered(text: str, question: dict[str, Any]) -> bool:
    """The same proximity rule the document scorer uses, on a query result."""

    haystack = text.casefold()
    positions = [
        found
        for subject in (item.casefold() for item in question["subject"])
        for found in _occurrences(haystack, subject)
    ]
    if not positions:
        return False
    for answer in question["answers"]:
        for found in _occurrences(haystack, answer.casefold()):
            if any(abs(found - anchor) <= WINDOW for anchor in positions):
                return True
    return False


def _occurrences(haystack: str, needle: str) -> list[int]:
    """Where a needle appears as its own word, not inside a longer one.

    A raw substring search answered "which exception signals a decode
    failure" with `UnicodeDecodeError`, and would answer "how many retries"
    with any number containing 10. The boundary is only applied where the
    needle actually has one: a needle starting or ending in punctuation, like
    a regular expression or a dotted version, keeps that end open.
    """

    left = r"(?<!\w)" if needle[:1].isalnum() or needle[:1] == "_" else ""
    right = r"(?!\w)" if needle[-1:].isalnum() or needle[-1:] == "_" else ""
    pattern = re.compile(left + re.escape(needle) + right)
    return [match.start() for match in pattern.finditer(haystack)]


def _depth(lines: list[str], question: dict[str, Any]) -> int | None:
    """Rows an agent must read before the answer is in hand, or None.

    Counted by reading down the result the way a caller does, rather than
    scoring the whole block at once: a question answered by the first row and
    one answered by the twentieth cost different amounts and used to score the
    same.
    """

    for index in range(1, len(lines) + 1):
        if _answered("\n".join(lines[:index]), question):
            return index
    return None


def _query(question: dict[str, Any]) -> str:
    """An FTS query built from the question, never from the answer."""

    terms = []
    for subject in question["subject"]:
        cleaned = re.sub(r"[^\w\s]", " ", subject).strip()
        if cleaned:
            terms.append(f'"{cleaned}"')
    return " OR ".join(terms)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--root", type=Path, help="The analysed repository, for the source size.")
    parser.add_argument("--show", type=int, default=8)
    arguments = parser.parse_args()

    payload = json.loads(arguments.questions.read_text(encoding="utf-8"))
    ledger = EvidenceLedger(arguments.state_dir / "evidence.sqlite3")
    latest = ledger.latest_snapshot()
    if latest is None:
        print("no snapshot in that state directory")
        return 0
    snapshot_id = str(latest["snapshot_id"])

    document = arguments.state_dir / "spec.md"
    document_size = document.stat().st_size if document.exists() else 0
    source_size = 0
    if arguments.root and arguments.root.exists():
        for record in ledger.list_files(snapshot_id):
            candidate = arguments.root / str(record["path"])
            if candidate.exists():
                source_size += candidate.stat().st_size

    answered = 0
    total_cost = 0
    unanswered: list[str] = []
    ranked_out: list[str] = []
    depths: list[int] = []
    for question in payload["questions"]:
        query = _query(question)
        rows = ledger.search_claims(snapshot_id, query, limit=QUERY_LIMIT)
        declared = ledger.search_declarations(snapshot_id, query, limit=QUERY_LIMIT)
        # Exactly what `open-skeleton search` prints, so the cost recorded
        # is the cost an agent actually pays rather than a friendlier
        # rendering of it.
        rendered = "\n".join(
            [str(row.get("claim", "")) for row in rows]
            + [
                f"{symbol['qualified_name']}: {line}  ({symbol['path']})"
                for symbol in declared
                for line in symbol["declares"].splitlines()
            ]
        )
        lines = rendered.splitlines()
        # How far down an agent reads before it has the answer. It stops
        # there, so the cost of a question is the text above the answer and
        # not the whole result.
        depth = _depth(lines, question)
        if depth is not None:
            answered += 1
            depths.append(depth)
            total_cost += len("\n".join(lines[:depth]))
            continue

        total_cost += len(rendered)
        # The same query, far deeper. If the answer is there it was ranked
        # out, which is a scoring problem; if it is not, nothing indexed it,
        # which is a reading problem. The two need opposite work and the
        # answered count alone cannot tell them apart.
        deep_rows = ledger.search_claims(snapshot_id, query, limit=DEEP_LIMIT)
        deep_declared = ledger.search_declarations(snapshot_id, query, limit=DEEP_LIMIT)
        deep = "\n".join(
            [str(row.get("claim", "")) for row in deep_rows]
            + [
                f"{symbol['qualified_name']}: {line}  ({symbol['path']})"
                for symbol in deep_declared
                for line in symbol["declares"].splitlines()
            ]
        )
        if _answered(deep, question):
            ranked_out.append(str(question["id"]))
        else:
            unanswered.append(str(question["id"]))

    count = max(1, len(payload["questions"]))
    average = total_cost / count
    print(f"\n## Retrieval cost on {payload.get('fixture', arguments.state_dir.name)}\n")
    print(f"questions            {count}")
    print(f"answered by query    {answered}/{count}")
    print(
        f"average query result {average:,.0f} characters (~{average / CHARACTERS_PER_TOKEN:,.0f} tokens)"
    )
    if document_size:
        print(
            f"whole document       {document_size:,} characters ({document_size / max(1, average):,.0f}x)"
        )
    if source_size:
        print(
            f"whole source         {source_size:,} characters ({source_size / max(1, average):,.0f}x)"
        )
    if depths:
        print(f"rows read to answer  {sum(depths) / len(depths):.1f} average, {max(depths)} worst")
    if ranked_out:
        print(
            f"\nranked out (in the index, below the cut): {', '.join(ranked_out[: arguments.show])}"
        )
    if unanswered:
        print(f"not indexed at all: {', '.join(unanswered[: arguments.show])}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
