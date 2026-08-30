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
FENCE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")
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


def _heading_matches(document: str) -> list[re.Match[str]]:
    """Find Markdown headings while ignoring examples inside code fences."""

    matches: list[re.Match[str]] = []
    active_fence: tuple[str, int] | None = None
    offset = 0
    for line in document.splitlines(keepends=True):
        raw_line = line.rstrip("\r\n")
        fence = FENCE.match(raw_line)
        if fence is not None:
            marker = fence.group(1)
            remainder = fence.group(2)
            if active_fence is None:
                active_fence = (marker[0], len(marker))
            elif (
                marker[0] == active_fence[0]
                and len(marker) >= active_fence[1]
                and not remainder.strip()
            ):
                active_fence = None
            offset += len(line)
            continue
        if active_fence is None and (match := HEADING.match(document, offset)) is not None:
            matches.append(match)
        offset += len(line)
    return matches


def _sections(document: str) -> list[Section]:
    matches = _heading_matches(document)
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


def _section_bodies(document: str) -> list[str]:
    """The text under each heading, in the same order `_sections` returns."""

    matches = _heading_matches(document)
    bodies: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(document)
        bodies.append(document[match.end() : end])
    return bodies


def _overlap(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left)


CODE_SPAN = re.compile(r"`([^`\n]{2,80})`")


def _distinctive_terms(sections: list[Section], bodies: list[str]) -> list[frozenset[str]]:
    """The names each section uses that few other sections use.

    Title matching answers "did the other document label this the same way",
    which is not the question a reader is asking. A baseline section headed
    "Endpoint Catalog and Response Conventions" shares no title word with a
    candidate section headed "HTTP Interface", and scored as an untreated
    subject across 1,333 lines of endpoint tables.

    A name used by most sections -- the repository's own name, `main.py` --
    pairs everything with everything, so a term counts only where it is rare
    enough across the baseline to identify a subject.
    """

    per_section = [
        frozenset(
            span.strip()
            for span in CODE_SPAN.findall(body)
            if 2 < len(span.strip()) <= 80 and not span.strip().isdigit()
        )
        for body in bodies
    ]
    frequency: dict[str, int] = {}
    for terms in per_section:
        for term in terms:
            frequency[term] = frequency.get(term, 0) + 1
    ceiling = max(1, len(sections) // 4)
    return [
        frozenset(term for term in terms if frequency[term] <= ceiling) for terms in per_section
    ]


def _content_covered(terms: frozenset[str], candidate_text: str, threshold: float) -> bool | None:
    """Whether the candidate names enough of a section's distinctive terms.

    Returns None when the section carries too few distinctive names to judge
    at all -- a subject argued entirely in prose is not evidence either way,
    and scoring it as a miss would be the same error in the other direction.
    """

    if len(terms) < 3:
        return None
    hits = sum(1 for term in terms if term.casefold() in candidate_text)
    return hits / len(terms) >= threshold


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

    baseline_text = args.baseline.read_text(encoding="utf-8")
    candidate_text = args.candidate.read_text(encoding="utf-8")
    baseline = _sections(baseline_text)
    candidate = _sections(candidate_text)
    baseline_terms = _distinctive_terms(baseline, _section_bodies(baseline_text))
    folded_candidate = candidate_text.casefold()
    candidate_keywords = [item.keywords for item in candidate]

    covered: list[tuple[Section, str]] = []
    missing: list[Section] = []
    content_covered: list[Section] = []
    content_absent: list[Section] = []
    unjudgeable: list[Section] = []
    for section, terms in zip(baseline, baseline_terms, strict=True):
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

        verdict = _content_covered(terms, folded_candidate, args.threshold)
        if verdict is None:
            unjudgeable.append(section)
        elif verdict:
            content_covered.append(section)
        else:
            content_absent.append(section)

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

    judged_by_content = len(content_covered) + len(content_absent)
    lines.extend(
        [
            "\n## The same question asked of the content\n\n",
            (
                "The table above pairs sections by the words in their titles, which "
                "answers whether the two documents *label* a subject the same way. A "
                'baseline section headed "Endpoint Catalog and Response Conventions" '
                'shares no title word with one headed "HTTP Interface" and is scored '
                "as untreated across a thousand lines of endpoint tables.\n\n"
                "This asks instead whether the candidate names the distinctive terms a "
                "baseline section uses -- anywhere in the document, under any heading. "
                "A term most baseline sections already use cannot identify a subject, "
                "so only rare ones count.\n\n"
            ),
            "| Measure | Sections | Share |\n",
            "|---|---:|---:|\n",
            (
                f"| Subject's terms present in the candidate | {len(content_covered):,} | "
                f"{len(content_covered) / judged_by_content:.1%} |\n"
                if judged_by_content
                else "| Subject's terms present in the candidate | 0 | n/a |\n"
            ),
            (
                f"| Subject's terms absent | {len(content_absent):,} | "
                f"{len(content_absent) / judged_by_content:.1%} |\n"
                if judged_by_content
                else "| Subject's terms absent | 0 | n/a |\n"
            ),
            "\n",
            (
                f"A further {len(unjudgeable):,} sections carry fewer than three "
                "distinctive names and are argued almost entirely in prose. They are "
                "reported here rather than scored: a subject with nothing nameable in "
                "it is not evidence in either direction, and counting it as a miss "
                "would repeat the title-matching error pointing the other way.\n\n"
            ),
        ]
    )

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
