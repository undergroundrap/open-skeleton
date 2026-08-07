# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Run the analyzer over every package in a directory and report what broke.

Two failures matter here and neither is visible from a fixture suite.

The first is a crash. An installed library once raised `KeyError` on a module
level counter and abandoned the whole package, and the shape that caused it --
`global n` on a name that is not a mutable container -- appears in no fixture
anyone would think to write. A crash is the worst outcome available, because
the repository produces nothing at all and the reason is a single statement.

The second is silence. A package that analyzes cleanly and says nothing is not
a simple package; it is a taxonomy with no category for what that code
contains. Two claims for `attrs` looked like a quiet library and was a
specification vocabulary built entirely from web applications, with no notion
of a public surface or a scheduled removal.

Census categories are excluded from the count because they are emitted for
every repository whether or not anything was found, so including them would
let a package that says nothing about itself still score as speaking.

Point this at a virtualenv's site-packages, a vendor directory, or any folder
of checkouts:

    python benchmarks/robustness/run_robustness.py --root .venv/Lib/site-packages
"""

from __future__ import annotations

import argparse
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.scanner import scan_repository

# Emitted for every repository regardless of what is in it, so they cannot
# distinguish a package the analyzer understood from one it did not.
CENSUS_CATEGORIES = frozenset(
    {"delivery_automation", "testing_census", "testing_gap", "language_census"}
)
MIN_SOURCE_FILES = 3


@dataclass(frozen=True, slots=True)
class Outcome:
    name: str
    source_files: int
    claims: int
    error: str = ""

    @property
    def per_file(self) -> float:
        return self.claims / self.source_files if self.source_files else 0.0


def _candidates(root: Path) -> list[Path]:
    """Directories holding source in any language this engine analyzes.

    The first version required a `*.py` file, which meant a harness built
    to give the Rust and TypeScript analyzers outside scrutiny examined one
    of six repositories and reported success. A filter that quietly drops
    what it was written to measure is worse than no filter.
    """

    return sorted(
        item
        for item in root.iterdir()
        if item.is_dir()
        and not item.name.endswith(("dist-info", "egg-info", "__pycache__"))
        and not item.name.startswith((".", "_"))
        and any(
            next(item.rglob(pattern), None) is not None
            for pattern in ("*.py", "*.rs", "*.ts", "*.tsx", "*.js", "*.jsx")
        )
    )


def _analyze(target: Path) -> Outcome:
    try:
        snapshot = scan_repository(target)
        sources = sum(1 for item in snapshot.files if item.role == "source")
        result = analyze_snapshot(snapshot)
    except Exception as exc:  # noqa: BLE001 — the point is to survive anything
        frames = traceback.extract_tb(sys.exc_info()[2])
        site = next(
            (
                f"{frame.filename.rsplit('open_skeleton', 1)[-1]}:{frame.lineno}"
                for frame in reversed(frames)
                if "open_skeleton" in frame.filename
            ),
            "unknown",
        )
        return Outcome(target.name, 0, 0, f"{type(exc).__name__} at {site}: {exc}"[:160])
    substantive = sum(1 for item in result.claims if item.category not in CENSUS_CATEGORIES)
    return Outcome(target.name, sources, substantive)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Directory of packages.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any package crashes or produces no substantive claim.",
    )
    arguments = parser.parse_args()

    targets = _candidates(arguments.root.expanduser().resolve(strict=True))
    outcomes = [_analyze(target) for target in targets]
    measured = [
        item for item in outcomes if not item.error and item.source_files >= MIN_SOURCE_FILES
    ]
    crashed = [item for item in outcomes if item.error]
    silent = [item for item in measured if item.claims == 0]

    print(f"packages examined: {len(outcomes):,}")
    print(f"  crashed:              {len(crashed):,}")
    print(f"  measured (>= {MIN_SOURCE_FILES} source files): {len(measured):,}")
    print(f"  measured but silent:  {len(silent):,}")

    if crashed:
        print("\nCRASHED — a whole package produced nothing:")
        for item in crashed:
            print(f"  {item.name}: {item.error}")
    if silent:
        print("\nSILENT — analyzed cleanly and said nothing about itself:")
        for item in silent:
            print(f"  {item.name} ({item.source_files} source files)")

    if measured:
        ranked = sorted(measured, key=lambda item: item.per_file)
        print("\nlowest claim density:")
        for item in ranked[:5]:
            print(f"  {item.name:24s} {item.source_files:4d} files  {item.per_file:5.2f}/file")
        print("highest claim density:")
        for item in ranked[-3:]:
            print(f"  {item.name:24s} {item.source_files:4d} files  {item.per_file:5.2f}/file")
        spread = ranked[-1].per_file / ranked[0].per_file if ranked[0].per_file else 0.0
        print(f"\nspread: {spread:.0f}x")
        print(
            "Spread is not by itself a defect. A parser has fewer architectural "
            "facts than a CLI framework, and a library whose whole job is reading "
            "environment settings should score high on exactly that."
        )

    return 1 if arguments.strict and (crashed or silent) else 0


if __name__ == "__main__":
    raise SystemExit(main())
