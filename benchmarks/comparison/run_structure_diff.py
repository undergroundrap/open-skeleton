# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Compare what two specifications are *about*, section by section.

The fact-coverage benchmark reads backticked code spans. That makes it blind
to whole categories of content: a baseline can devote nine subsections and
forty tables to a topic this engine never attempts, and every fact in them
sits in prose, so coverage stays high while the gap stays invisible.

This inverts that. It reads the heading tree of both documents, measures how
much each section actually contains, and matches baseline topics against
candidate topics by the words in their titles. What comes back unmatched is a
subject the baseline treats and this engine does not — ranked by how much the
baseline spent on it, because that is the order worth closing them in.

Matching is lexical and therefore approximate in both directions: a section
covered under a different name reads as a gap, and a shared word can pair two
sections that discuss different things. The report prints the matched title
beside each pairing so a reader can overrule it, and that judgement is the
point of the output rather than the percentage.

    python benchmarks/comparison/run_structure_diff.py \\
        --baseline <tech_spec.md> --candidate <spec.md> --output-dir <dir>
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
LEADING_NUMBER = re.compile(r"^[\d.]+\s*")
WORD = re.compile(r"[A-Za-z][A-Za-z-]{2,}")
# Words that pair unrelated sections because every specification uses them.
STOPWORDS = frozenset(
    {
        "and",
        "the",
        "for",
        "with",
        "その",
        "references",
        "overview",
        "details",
        "considerations",
        "requirements",
        "implementation",
        "technical",
        "system",
        "systems",
        "general",
        "other",
        "additional",
        "summary",
        "introduction",
        "section",
        "sections",
        "this",
        "that",
        "from",
        "into",
        "each",
        "what",
        "which",
    }
)


@dataclass
class Section:
    """One heading and everything under it until the next heading of any level."""

    level: int
    title: str
    words: int = 0
    table_rows: int = 0
    diagrams: int = 0
    code_blocks: int = 0
    keywords: frozenset[str] = field(default_factory=frozenset)

    @property
    def weight(self) -> int:
        """How much the document spent here.

        A table row carries more asserted fact per line than a sentence does,
        and a diagram more than either, so they are counted rather than left
        to be represented by their word count alone.
        """

        return self.words + self.table_rows * 6 + self.diagrams * 40 + self.code_blocks * 10


def _keywords(title: str) -> frozenset[str]:
    cleaned = LEADING_NUMBER.sub("", title).replace("&amp;", "&")
    return frozenset(
        word.casefold() for word in WORD.findall(cleaned) if word.casefold() not in STOPWORDS
    )


def _sections(document: str) -> list[Section]:
    matches = list(HEADING.finditer(document))
    sections: list[Section] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(document)
        body = document[start:end]
        title = match.group(2)
        sections.append(
            Section(
                level=len(match.group(1)),
                title=title,
                words=len(body.split()),
                table_rows=len(re.findall(r"^\|", body, re.M)),
                diagrams=body.count("```mermaid"),
                code_blocks=max(0, body.count("```") // 2),
                keywords=_keywords(title),
            )
        )
    return sections


def _overlap(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--candidate-label", default="Open Skeleton")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Share of a baseline heading's words that must appear in a candidate heading.",
    )
    parser.add_argument("--sample", type=int, default=30)
    args = parser.parse_args()

    baseline = _sections(args.baseline.read_text(encoding="utf-8"))
    candidate = _sections(args.candidate.read_text(encoding="utf-8"))
    candidate_keywords = [item.keywords for item in candidate]

    covered: list[tuple[Section, str]] = []
    missing: list[Section] = []
    for section in baseline:
        if not section.keywords:
            continue
        best_score, best_title = 0.0, ""
        for other, keywords in zip(candidate, candidate_keywords, strict=True):
            score = _overlap(section.keywords, keywords)
            if score > best_score:
                best_score, best_title = score, other.title
        if best_score >= args.threshold:
            covered.append((section, best_title))
        else:
            missing.append(section)

    judged = len(covered) + len(missing)
    covered_weight = sum(section.weight for section, _ in covered)
    missing_weight = sum(section.weight for section in missing)
    total_weight = covered_weight + missing_weight

    def pct(part: int, whole: int) -> str:
        return f"{part / whole:.1%}" if whole else "n/a"

    missing.sort(key=lambda item: -item.weight)
    lines = [
        "# Structural coverage\n\n",
        (
            "What the baseline is *about*, section by section, checked against "
            f"{args.candidate_label}. Fact coverage reads code spans and cannot "
            "see a topic discussed entirely in prose; this can.\n\n"
        ),
        "| Measure | Baseline | Matched | Share |\n|---|---:|---:|---:|\n",
        (
            f"| Headings with a subject | {judged:,} | {len(covered):,} | "
            f"{pct(len(covered), judged)} |\n"
        ),
        (
            f"| Weighted content | {total_weight:,} | {covered_weight:,} | "
            f"{pct(covered_weight, total_weight)} |\n\n"
        ),
        (
            "Weight counts a table row as six words, a diagram as forty and a "
            "code block as ten, because a table asserts more per line than a "
            "sentence does. It measures how much the baseline spent on a "
            "subject, not how much of that was worth spending.\n\n"
        ),
    ]

    if missing:
        lines.append(
            f"## Subjects the baseline treats and {args.candidate_label} does not "
            f"({len(missing):,})\n\nRanked by how much the baseline spent on each, "
            "which is the order worth closing them in.\n\n"
            "| Baseline heading | Words | Tables | Diagrams | Weight |\n"
            "|---|---:|---:|---:|---:|\n"
        )
        for section in missing[: args.sample]:
            title = section.title.replace("|", "\\|")
            lines.append(
                f"| {'#' * section.level} {title} | {section.words:,} | "
                f"{section.table_rows:,} | {section.diagrams:,} | {section.weight:,} |\n"
            )
        if len(missing) > args.sample:
            lines.append(f"\n_…and {len(missing) - args.sample:,} further headings._\n")
        lines.append("\n")

    lines.append(
        "## Method and its limits\n\n"
        "Matching is lexical: a baseline heading counts as covered when at "
        f"least {args.threshold:.0%} of its meaningful title words appear in "
        "some candidate heading. That is approximate in both directions — a "
        "subject covered under a different name reads as a gap, and a shared "
        "word can pair two sections that discuss different things. The "
        "matched title is printed beside each pairing in the JSON so the "
        "judgement can be overruled, and overruling it is the point of this "
        "report rather than the percentage.\n\n"
        "A heading counted as missing is not automatically work worth doing. "
        "Some of what a long-form baseline spends its length on is narrative "
        "this engine deliberately does not generate, and the right response to "
        "part of this list is to decide it is out of scope rather than to "
        "close it.\n"
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = "".join(lines)
    (args.output_dir / "structure-diff.md").write_text(report, encoding="utf-8", newline="\n")
    (args.output_dir / "structure-diff.json").write_text(
        json.dumps(
            {
                "headings_judged": judged,
                "headings_matched": len(covered),
                "weight_total": total_weight,
                "weight_matched": covered_weight,
                "matched": [
                    {"baseline": section.title, "candidate": title} for section, title in covered
                ],
                "missing": [
                    {
                        "title": section.title,
                        "level": section.level,
                        "words": section.words,
                        "table_rows": section.table_rows,
                        "diagrams": section.diagrams,
                        "weight": section.weight,
                    }
                    for section in missing
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(report[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
