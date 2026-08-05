# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Measure this engine against a supplied baseline specification.

Both documents describe the same repository at the same commit. Every number
here is counted from the two artifacts on disk — nothing is asserted, and the
script prints the command that reproduces each figure.

Usage:

    python benchmarks/comparison/run_comparison.py \\
        --repository <path to fixture> \\
        --baseline <path to the baseline tech_spec.md> \\
        --output-dir <where to write the report>

The baseline artifact is not redistributed with this repository. Supply your
own export to reproduce the comparison.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MERMAID_FENCE = "```mermaid"
FILE_REFERENCE = re.compile(r"`([\w./-]+\.(?:py|tsx|ts|js|jsx|css|md|json|toml|txt|rs))(:(\d+))?")


def _diagram_kinds(text: str) -> collections.Counter[str]:
    """Count Mermaid blocks by their declared diagram type."""

    kinds: collections.Counter[str] = collections.Counter()
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != MERMAID_FENCE:
            continue
        following = next(
            (
                lines[cursor].strip()
                for cursor in range(index + 1, index + 4)
                if lines[cursor].strip()
            ),
            "",
        )
        kinds[following.split()[0] if following else "unknown"] += 1
    return kinds


def _citations(text: str) -> tuple[int, int, int]:
    """Return total file references, those carrying a line, and distinct files."""

    matches = FILE_REFERENCE.findall(text)
    with_line = [item for item in matches if item[2]]
    return len(matches), len(with_line), len({item[0] for item in matches})


def _measure_open_skeleton(repository: Path, output_dir: Path) -> dict[str, Any]:
    """Run analyze and spec end to end, timing the whole path."""

    state = output_dir / "state"
    started = time.perf_counter()
    for command in (
        [
            sys.executable,
            "-m",
            "open_skeleton",
            "analyze",
            str(repository),
            "--state-dir",
            str(state),
            "--quiet",
        ],
        [
            sys.executable,
            "-m",
            "open_skeleton",
            "spec",
            str(repository),
            "--state-dir",
            str(state),
            "--verify",
            "--json",
        ],
    ):
        completed = subprocess.run(  # noqa: S603
            command, capture_output=True, text=True, check=False
        )
        if completed.returncode != 0:
            raise SystemExit(f"command failed: {' '.join(command)}\n{completed.stderr}")
        last = completed.stdout
    elapsed = time.perf_counter() - started

    summary = json.loads(last)
    document = (state / "spec.md").read_text(encoding="utf-8")
    total, with_line, distinct = _citations(document)
    return {
        "seconds": round(elapsed, 3),
        "words": len(document.split()),
        "sections": summary["sections"],
        "diagrams": dict(_diagram_kinds(document)),
        "diagram_total": sum(_diagram_kinds(document).values()),
        "file_references": total,
        "references_with_line": with_line,
        "distinct_files_cited": distinct,
        "machine_verified_citations": summary["citations"]["total_citations"],
        "citation_integrity": summary["citation_integrity"],
        "capabilities": summary["capabilities"],
        "capabilities_exercised": summary["capabilities_exercised"],
    }


def _measure_baseline(path: Path, seconds: float | None) -> dict[str, Any]:
    document = path.read_text(encoding="utf-8")
    total, with_line, distinct = _citations(document)
    kinds = _diagram_kinds(document)
    return {
        "seconds": seconds,
        "words": len(document.split()),
        "sections": len(re.findall(r"^#{1,2} ", document, flags=re.MULTILINE)),
        "diagrams": dict(kinds),
        "diagram_total": sum(kinds.values()),
        "file_references": total,
        "references_with_line": with_line,
        "distinct_files_cited": distinct,
        "machine_verified_citations": 0,
        "citation_integrity": None,
        "capabilities": None,
        "capabilities_exercised": None,
    }


def _render(ours: dict[str, Any], theirs: dict[str, Any], label: str) -> str:
    def row(name: str, key: str, fmt: str = "{:,}") -> str:
        left = ours.get(key)
        right = theirs.get(key)
        render = lambda value: "not reported" if value is None else fmt.format(value)  # noqa: E731
        return f"| {name} | {render(left)} | {render(right)} |\n"

    lines = [
        "# Specification comparison\n\n",
        (
            "Both documents describe the same repository at the same commit. "
            f"The baseline is {label}.\n\n"
        ),
        "| Measure | Open Skeleton | Baseline |\n|---|---:|---:|\n",
        row("Generation time (seconds)", "seconds", "{:,.1f}"),
        row("Diagrams", "diagram_total"),
        row("Words", "words"),
        row("File references", "file_references"),
        row("References carrying a line number", "references_with_line"),
        row("Citations verified against source hashes", "machine_verified_citations"),
    ]
    integrity = ours.get("citation_integrity")
    if integrity is not None:
        lines.append(f"| Citation integrity | {integrity:.1%} | not reported |\n")
    lines.append("\n## Diagrams by type\n\n| Type | Open Skeleton | Baseline |\n|---|---:|---:|\n")
    for kind in sorted(set(ours["diagrams"]) | set(theirs["diagrams"])):
        lines.append(
            f"| `{kind}` | {ours['diagrams'].get(kind, 0):,} | {theirs['diagrams'].get(kind, 0):,} |\n"
        )
    lines.append(
        "\n## What these numbers do and do not show\n\n"
        "Every figure above is counted from the two documents on disk by "
        "`benchmarks/comparison/run_comparison.py`. Re-run it to reproduce them.\n\n"
        "The comparison measures the shape of two artifacts describing one "
        "repository. It does not measure correctness of the baseline's prose, and "
        "it does not claim the two documents attempt the same scope: the baseline "
        "carries a requirements catalog and interface analysis this engine does "
        "not produce. Word count is reported because it is asked about, not "
        "because more words are better.\n\n"
        "The citation rows are the ones that matter. A reference carrying a line "
        "number can be checked by a reader; one that names only a file cannot. A "
        "citation pinned to a content hash can be checked by a machine, which is "
        "what `open-skeleton spec --verify` does on every run.\n"
    )
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--baseline-seconds",
        type=float,
        help="Wall time the baseline took, if known. Reported as supplied.",
    )
    parser.add_argument(
        "--baseline-label",
        default="a leading commercial code-intelligence platform",
        help="How the baseline is described in the rendered report.",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ours = _measure_open_skeleton(args.repository.resolve(strict=True), args.output_dir)
    theirs = _measure_baseline(args.baseline.resolve(strict=True), args.baseline_seconds)

    report = _render(ours, theirs, args.baseline_label)
    (args.output_dir / "comparison.md").write_text(report, encoding="utf-8", newline="\n")
    (args.output_dir / "comparison.json").write_text(
        json.dumps({"open_skeleton": ours, "baseline": theirs}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
