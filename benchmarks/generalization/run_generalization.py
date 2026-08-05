# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Measure whether this engine analyses repositories, or one repository.

Every accuracy benchmark here is pinned to a single fixture. That is the right
way to prove a claim is reproducible and the wrong way to prove a tool is
general: an engine tuned against one Python service will score perfectly on
that service while saying almost nothing about a Rust one, and no pinned
benchmark will ever notice.

This runs the whole pipeline over several repositories and reports what each
one yielded per file. The number that matters is not any single repository's
score but the spread between them. A tool that generalises produces claims at
a broadly similar rate across languages it says it supports; a tool fitted to
one produces a spike there and a flat line everywhere else.

    python benchmarks/generalization/run_generalization.py \\
        --repo path/to/one --repo path/to/two --output-dir <dir>

Claim yield is a proxy, not a quality measure. A repository can be small,
simple, or genuinely have little worth saying about it, and a low yield there
is correct rather than a failure. What the spread cannot explain away is the
same analyzer finding thirty times more per file in one language than another.

Yield alone also cannot separate a weak analyzer from clean code, and that
distinction decides what to do next. warmboot yields little partly because
the Rust analyzer extracts less and partly because the crate has no global
mutable state to find — one is work and the other is nothing at all. The
report therefore shows which analyzer covered which files, so a language with
no analyzer at all is visible as coverage rather than inferred from a low
score.
"""

from __future__ import annotations

import argparse
import collections
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository

# Languages some analyzer claims eligibility for. Anything else in a tree is
# scanned and counted but never read for meaning.
_ANALYZED_LANGUAGES = frozenset(
    {"Python", "Rust", "TypeScript", "TypeScript JSX", "JavaScript", "JavaScript JSX", "Hum"}
)


def _measure(root: Path) -> dict[str, Any]:
    # `.` has no name of its own, so resolve before reading one off the path.
    root = root.resolve()
    state = Path(tempfile.mkdtemp(prefix="open-skeleton-generalization-"))
    try:
        started = time.perf_counter()
        snapshot = scan_repository(root)
        result = analyze_snapshot(snapshot)
        elapsed = time.perf_counter() - started

        ledger = EvidenceLedger(state / "evidence.sqlite3")
        ledger.save_snapshot(snapshot)
        ledger.save_analysis(result)

        languages = collections.Counter(str(item.language) for item in snapshot.files)
        categories = collections.Counter(item.category for item in result.claims)
        analyzed = sum(item.analyzed_files for item in result.coverage)
        by_analyzer = {
            str(item.analyzer): {
                "eligible": item.eligible_files,
                "analyzed": item.analyzed_files,
                "claims": sum(1 for claim in result.claims if claim.produced_by == item.analyzer),
            }
            for item in result.coverage
        }
        # A language present in the tree that no analyzer declared eligible is
        # the clearest possible statement of a missing analyzer, and it is
        # invisible in a claim count.
        covered = {
            str(item.language) for item in snapshot.files if item.language in _ANALYZED_LANGUAGES
        }
        unanalyzed = {
            name: count
            for name, count in languages.items()
            if name not in covered
            and name not in {"Unknown", "Text", "Markdown", "JSON", "YAML", "TOML"}
        }
        files = len(snapshot.files) or 1
        return {
            "repository": root.name,
            "files": len(snapshot.files),
            "analyzed_files": analyzed,
            "claims": len(result.claims),
            "claims_per_file": round(len(result.claims) / files, 3),
            "distinct_categories": len(categories),
            "symbols": len(result.symbols),
            "evidence": len(result.evidence),
            "seconds": round(elapsed, 2),
            "languages": dict(languages.most_common(6)),
            "by_analyzer": by_analyzer,
            "languages_without_an_analyzer": unanalyzed,
            "categories": dict(categories.most_common()),
        }
    finally:
        shutil.rmtree(state, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    rows = [_measure(path) for path in args.repo if path.is_dir()]
    if not rows:
        print("no readable repositories given")
        return 1

    rows.sort(key=lambda item: -float(item["claims_per_file"]))
    best = float(rows[0]["claims_per_file"]) or 1.0
    worst = float(rows[-1]["claims_per_file"])
    spread = best / worst if worst else float("inf")

    lines = [
        "# Generalization\n\n",
        (
            "The same pipeline over several repositories. The number that matters "
            "is the spread between them, not any single score: an engine fitted "
            "to one repository produces a spike there and a flat line "
            "everywhere else, and no pinned benchmark will ever notice.\n\n"
        ),
        "| Repository | Files | Claims | Per file | Categories | Seconds | Dominant languages |\n",
        "|---|---:|---:|---:|---:|---:|---|\n",
    ]
    for row in rows:
        languages = ", ".join(f"{name} {count}" for name, count in row["languages"].items())
        lines.append(
            f"| {row['repository']} | {row['files']:,} | {row['claims']:,} | "
            f"{row['claims_per_file']:.2f} | {row['distinct_categories']} | "
            f"{row['seconds']:.1f} | {languages} |\n"
        )
    lines.append(f"\n**Spread: {spread:.0f}x** between the highest and lowest yield per file.\n\n")

    # Yield per repository mixes analyzer strength with what the repository
    # happens to contain. Yield per file an analyzer actually read does not:
    # it is the same analyzer measured against its own input, so a low number
    # there is missing extraction rather than clean code.
    per_analyzer: dict[str, dict[str, int]] = {}
    for row in rows:
        for name, stats in row["by_analyzer"].items():
            entry = per_analyzer.setdefault(name, {"analyzed": 0, "claims": 0})
            entry["analyzed"] += int(stats["analyzed"])
            entry["claims"] += int(stats["claims"])
    active = {name: stats for name, stats in per_analyzer.items() if stats["analyzed"]}
    if active:
        lines.append(
            "## Yield per file each analyzer actually read\n\n"
            "The table above mixes analyzer strength with what a repository "
            "happens to contain: warmboot yields little partly because the Rust "
            "analyzer extracts less and partly because the crate has no global "
            "mutable state to find. This measures each analyzer against its own "
            "input, where that confusion cannot arise.\n\n"
            "| Analyzer | Files read | Claims | Per file |\n|---|---:|---:|---:|\n"
        )
        for name, stats in sorted(
            active.items(), key=lambda item: -item[1]["claims"] / (item[1]["analyzed"] or 1)
        ):
            rate = stats["claims"] / stats["analyzed"]
            lines.append(f"| {name} | {stats['analyzed']:,} | {stats['claims']:,} | {rate:.2f} |\n")
        rates = [s["claims"] / s["analyzed"] for s in active.values() if s["claims"]]
        if len(rates) > 1:
            lines.append(
                f"\n**Analyzer spread: {max(rates) / min(rates):.0f}x.** This is the "
                "number that describes the tool rather than the repositories.\n\n"
            )

    universe: collections.Counter[str] = collections.Counter()
    for row in rows:
        universe.update(row["categories"].keys())
    only_one = sorted(name for name, count in universe.items() if count == 1)
    if only_one:
        lines.append(
            f"## Categories produced for exactly one repository ({len(only_one)})\n\n"
            "A claim category that fires for a single repository is either a "
            "genuinely rare property or an analyzer written against that "
            "repository. Which one it is has to be decided by reading it.\n\n"
            + "".join(f"- `{name}`\n" for name in only_one)
            + "\n"
        )

    lines.append(
        "## Method and its limits\n\n"
        "Claim yield is a proxy. A repository can be small, simple, or genuinely "
        "have little worth saying about it, and a low yield there is correct "
        "rather than a failure — so no threshold here is a pass mark. What the "
        "spread cannot explain away is the same analyzer finding many times more "
        "per file in one language than in another, which is a statement about "
        "the analyzer and not about the code it read.\n"
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = "".join(lines)
    (args.output_dir / "generalization.md").write_text(report, encoding="utf-8", newline="\n")
    (args.output_dir / "generalization.json").write_text(
        json.dumps({"repositories": rows, "spread": spread}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
