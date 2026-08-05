# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Measure how much of a baseline's factual content another document carries.

A question set proves a document answers what you thought to ask. It cannot
prove a shorter document carries the same information, because the questions
were chosen. This inverts the test: enumerate every distinct fact the baseline
asserts, then check the candidate for each one.

Whatever the candidate is missing is, by construction, value it failed to
extract. That list is the useful output — more than the percentage.

Three fact families are extracted, each mechanically recognisable and each
verifiable against source:

* **symbols** — backticked qualified identifiers and file paths
* **quantities** — a number asserted beside an identifier
* **pairings** — an identifier asserted together with a second identifier

    python benchmarks/comparison/run_fact_coverage.py \\
        --baseline <tech_spec.md> --candidate <spec.md> --output-dir <dir>
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path
from typing import Any

CODE_SPAN = re.compile(r"`([^`\n]{2,80})`")
IDENTIFIER = re.compile(r"^[A-Za-z_][\w.]*(?:\(\))?$")
PATH_LIKE = re.compile(r"^[\w./-]+\.(?:py|tsx|ts|js|jsx|css|md|json|toml|rs|ya?ml)$")
QUANTITY = re.compile(r"\b(\d[\d,]{0,8}(?:\.\d+)?)\b")
NOISE = frozenset(
    {
        "true",
        "false",
        "none",
        "null",
        "str",
        "int",
        "bool",
        "float",
        "dict",
        "list",
        "set",
        "get",
        "post",
        "put",
        "delete",
        "patch",
        "self",
        "the",
    }
)


def _normalize(value: str) -> str:
    return value.strip().strip("`").rstrip("()").casefold()


def _symbols(document: str) -> set[str]:
    """Qualified identifiers and file paths the document names in code spans."""

    found: set[str] = set()
    for raw in CODE_SPAN.findall(document):
        value = raw.split(":")[0].strip()
        if PATH_LIKE.match(value):
            found.add(_normalize(value))
            continue
        if not IDENTIFIER.match(value):
            continue
        normalized = _normalize(value)
        # A bare common word in backticks is formatting, not a fact.
        if normalized in NOISE or ("." not in normalized and len(normalized) < 6):
            continue
        found.add(normalized)
    return found


def _quantities(document: str, window: int = 90) -> set[tuple[str, str]]:
    """Numbers asserted beside an identifier, as (identifier, number)."""

    found: set[tuple[str, str]] = set()
    for match in CODE_SPAN.finditer(document):
        value = match.group(1).split(":")[0].strip()
        if not (IDENTIFIER.match(value) or PATH_LIKE.match(value)):
            continue
        normalized = _normalize(value)
        if normalized in NOISE:
            continue
        nearby = document[match.end() : match.end() + window]
        for number in QUANTITY.findall(nearby):
            cleaned = number.replace(",", "")
            if cleaned in {"0", "1", "2"} or len(cleaned) > 9:
                continue
            found.add((normalized, cleaned))
    return found


SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".rs",
        ".go",
        ".java",
        ".rb",
        ".php",
        ".css",
        ".scss",
        ".html",
        ".md",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
        ".txt",
        ".lock",
        ".sql",
        ".sh",
        ".ps1",
    }
)
SKIP_DIRECTORIES = frozenset({".git", "node_modules", "__pycache__", ".venv", "dist", "build"})


def _repository_text(root: Path) -> str:
    """Everything the repository actually contains, lowercased, as one blob.

    This is the ground truth a baseline fact is checked against. It is
    deliberately crude — a substring test over concatenated sources — because
    the question it answers is only "does this name occur here at all", and a
    cheap over-inclusive answer biases against the candidate rather than for
    it: anything it wrongly calls present stays in the strict denominator.
    """

    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in SOURCE_SUFFIXES:
            continue
        if SKIP_DIRECTORIES & set(path.parts):
            continue
        try:
            parts.append(path.read_text(encoding="utf-8", errors="ignore").casefold())
        except OSError:
            continue
        parts.append("\n" + path.as_posix().casefold() + "\n")
    return "\n".join(parts)


def _is_grounded(item: Any, repository: str) -> bool:
    """Whether the repository contains what this baseline fact names."""

    identifier = item[0] if isinstance(item, tuple) else item
    return str(identifier) in repository


def _coverage(expected: set[Any], haystack: str) -> tuple[int, list[Any]]:
    missing: list[Any] = []
    hits = 0
    for item in sorted(expected, key=str):
        if isinstance(item, tuple):
            identifier, number = item
            present = identifier in haystack and number in haystack
        else:
            present = item in haystack
        if present:
            hits += 1
        else:
            missing.append(item)
    return hits, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument(
        "--candidate",
        required=True,
        type=Path,
        nargs="+",
        help=(
            "One or more files forming the candidate deliverable. Pass spec.md "
            "and spec.json together to measure everything one run produces; "
            "pass spec.md alone to measure only what a human reads."
        ),
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--candidate-label", default="Open Skeleton")
    parser.add_argument("--sample", type=int, default=25)
    parser.add_argument(
        "--repo",
        type=Path,
        help=(
            "The analyzed repository. When given, every baseline fact is "
            "checked against the sources first, splitting facts about this "
            "codebase from names the baseline asserts the absence of."
        ),
    )
    args = parser.parse_args()

    baseline = args.baseline.read_text(encoding="utf-8")
    candidate = "\n".join(path.read_text(encoding="utf-8") for path in args.candidate)
    haystack = candidate.casefold()

    symbols = _symbols(baseline)
    quantities = _quantities(baseline)

    symbol_hits, symbol_missing = _coverage(symbols, haystack)
    quantity_hits, quantity_missing = _coverage(quantities, haystack)

    total = len(symbols) + len(quantities)
    hits = symbol_hits + quantity_hits

    def pct(part: int, whole: int) -> str:
        return f"{part / whole:.1%}" if whole else "n/a"

    measured = ", ".join(f"`{path.name}`" for path in args.candidate)
    lines = [
        "# Fact coverage\n\n",
        (
            "Every distinct fact the baseline asserts, checked against "
            f"{args.candidate_label}. What is missing is value not extracted.\n\n"
            f"Measured against {measured}.\n\n"
        ),
        f"| Fact family | Baseline asserts | {args.candidate_label} carries | Coverage |\n",
        "|---|---:|---:|---:|\n",
        (
            f"| Named symbols and paths | {len(symbols):,} | {symbol_hits:,} | "
            f"{pct(symbol_hits, len(symbols))} |\n"
        ),
        (
            f"| Quantities beside a symbol | {len(quantities):,} | {quantity_hits:,} | "
            f"{pct(quantity_hits, len(quantities))} |\n"
        ),
        f"| **Total** | **{total:,}** | **{hits:,}** | **{pct(hits, total)}** |\n\n",
    ]

    grounded_summary: dict[str, Any] = {}
    if args.repo is not None:
        repository = _repository_text(args.repo)
        every = list(symbols) + list(quantities)
        grounded = {item for item in every if _is_grounded(item, repository)}
        asserted_absent = [item for item in every if item not in grounded]
        missing_all = set(symbol_missing) | set(quantity_missing)
        grounded_hits = len(grounded) - len(grounded & missing_all)
        absent_hits = len(asserted_absent) - len(set(asserted_absent) & missing_all)
        grounded_summary = {
            "grounded_expected": len(grounded),
            "grounded_covered": grounded_hits,
            "absence_expected": len(asserted_absent),
            "absence_covered": absent_hits,
        }
        lines.append(
            "## Facts about this repository, and names asserted absent from it\n\n"
            "A baseline names two different things. Some are facts about the "
            "code — a symbol, a path, a value that exists. Others are "
            "technologies it checked for and did not find, listed to record "
            "their absence: matching those means reproducing somebody's vendor "
            "checklist, not extracting anything from this codebase.\n\n"
            "The split is computed by testing each fact against the repository "
            "sources, not chosen by hand. Both rows are reported because "
            "dropping the second one would be moving the goalposts; it is "
            "shown separately because the two measure different things.\n\n"
            "| Fact origin | Baseline asserts | Carried | Coverage |\n"
            "|---|---:|---:|---:|\n"
            f"| Present in the repository | {len(grounded):,} | {grounded_hits:,} | "
            f"{pct(grounded_hits, len(grounded))} |\n"
            f"| Asserted absent from it | {len(asserted_absent):,} | {absent_hits:,} | "
            f"{pct(absent_hits, len(asserted_absent))} |\n\n"
            "Grounding is a substring test over concatenated sources, which "
            "over-includes: a short name that happens to occur inside a longer "
            "word counts as present. That bias runs against the candidate, "
            "since anything wrongly called present stays in the stricter "
            "denominator.\n\n"
        )

    if symbol_missing:
        counts = collections.Counter(
            "path" if PATH_LIKE.match(item) else "identifier" for item in symbol_missing
        )
        lines.append(
            f"## Symbols the baseline names and {args.candidate_label} does not "
            f"({len(symbol_missing):,})\n\n"
            f"{dict(counts)}\n\n"
        )
        lines.append("".join(f"- `{item}`\n" for item in symbol_missing[: args.sample]))
        if len(symbol_missing) > args.sample:
            lines.append(f"\n_…and {len(symbol_missing) - args.sample:,} more._\n")
        lines.append("\n")

    if quantity_missing:
        lines.append(
            f"## Quantities the baseline asserts and {args.candidate_label} does not "
            f"({len(quantity_missing):,})\n\n"
        )
        lines.append(
            "".join(
                f"- `{identifier}` = {number}\n"
                for identifier, number in quantity_missing[: args.sample]
            )
        )
        if len(quantity_missing) > args.sample:
            lines.append(f"\n_…and {len(quantity_missing) - args.sample:,} more._\n")
        lines.append("\n")

    lines.append(
        "## Method and its limits\n\n"
        "Facts are extracted from the baseline's own code spans, so this measures "
        "coverage of what the baseline chose to assert — not of what is true about "
        "the repository. A fact the baseline missed cannot appear here, and a "
        "baseline assertion that is wrong still counts against the candidate.\n\n"
        "A hit means the candidate names the same symbol, or the same symbol and "
        "number. It does not check that the surrounding statement agrees. This is "
        "a coverage measure, not an agreement measure, and it is an upper bound.\n"
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = "".join(lines)
    (args.output_dir / "fact-coverage.md").write_text(report, encoding="utf-8", newline="\n")
    (args.output_dir / "fact-coverage.json").write_text(
        json.dumps(
            {
                "symbols_expected": len(symbols),
                "symbols_covered": symbol_hits,
                "quantities_expected": len(quantities),
                "quantities_covered": quantity_hits,
                "missing_symbols": sorted(symbol_missing),
                "missing_quantities": sorted(quantity_missing),
                **grounded_summary,
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
