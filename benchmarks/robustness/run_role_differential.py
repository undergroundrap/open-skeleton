# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Relocate real source into a benchmark directory and see what still lies.

A claim that describes the product while resting only on a suite, a benchmark
or an example is this engine's most repeated defect: found and fixed five
times in five readers, each time in whichever analyzer happened to be open.
The audit detects it, but only where a repository already has the shape. Five
of the twelve corpus repositories keep no example app and no reference
implementation, so for those categories the audit is silent whether the rule
is covered or merely untested -- and those two look identical from outside.

This asks directly instead of waiting. It copies a repository's real source
under `benchmarks/` and again under `tests/`, runs the pipeline, and reports
any claim in a production category whose every receipt is a file that
exercises the system rather than being it. The content is unchanged and known
good; only the role differs, so anything reported is the reader ignoring role.

That is how four categories were found on three languages with no repository
to reveal them: `hardcoded_endpoint`, `failure_surface`, `ui_state`, and a
Rust `error_surface` guard that named the test role and let every benchmark
through.

    python benchmarks/robustness/run_role_differential.py --source src/open_skeleton
    python benchmarks/robustness/run_role_differential.py --source a --source b

Exit status is non-zero when anything is still filed as the product, so this
is usable as a gate.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import traceback
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.audit import PRODUCTION_CATEGORIES
from open_skeleton.policy import exercises_the_product
from open_skeleton.scanner import scan_repository

# Directories a copy should not carry: build output and dependencies are not
# the source under test, and they make the relocation slow enough to skip.
IGNORED = ("__pycache__", "node_modules", "target", "dist", "build", ".git")

# The roles a relocated tree can be given. Both are needed: a reader that
# names one of them, as the Rust error-surface guard did, passes under the
# role it knows and fails under the other.
ROLE_DIRECTORIES = ("benchmarks", "tests")


def misfiled(root: Path) -> Counter[str]:
    """Claims about the product resting only on files that exercise it."""

    snapshot = scan_repository(root)
    if not snapshot.files:
        return Counter()
    result = analyze_snapshot(snapshot)
    role_by_path = {item.path: item.role for item in snapshot.files}
    path_by_evidence = {item.evidence_id: item.path for item in result.evidence}

    found: Counter[str] = Counter()
    for claim in result.claims:
        if claim.category not in PRODUCTION_CATEGORIES:
            continue
        paths = {path_by_evidence.get(item) for item in claim.supporting_evidence}
        paths.discard(None)
        if not paths:
            # A repository-wide census names no file, so it cannot be read
            # this way. `no-file-evidence` in the audit is the check for that.
            continue
        if all(exercises_the_product(role_by_path.get(path or "")) for path in paths):
            found[claim.category] += 1
    return found


def differential(sources: list[Path], show: int) -> tuple[int, int]:
    checked = leaked = 0
    for source in sources:
        if not source.exists():
            print(f"MISSING {source}", flush=True)
            continue
        for role_directory in ROLE_DIRECTORIES:
            try:
                with TemporaryDirectory() as temporary:
                    destination = Path(temporary) / role_directory / source.name
                    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*IGNORED))
                    found = misfiled(Path(temporary))
                checked += 1
                if found:
                    leaked += 1
                    print(
                        f"MISFILED {source.name} under {role_directory}/: "
                        f"{sum(found.values())} claim(s) describe the product",
                        flush=True,
                    )
                    for category, count in found.most_common(show):
                        print(f"   {category} ({count})", flush=True)
            except Exception:  # noqa: BLE001 - a sweep reports failures, it does not raise
                print(f"CRASH {source.name} under {role_directory}/", flush=True)
                print("   " + traceback.format_exc().strip().splitlines()[-1], flush=True)
    return checked, leaked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        action="append",
        default=[],
        help="A directory of real source to relocate; repeatable.",
    )
    parser.add_argument("--show", type=int, default=8, help="Categories printed per relocation.")
    arguments = parser.parse_args()

    if not arguments.source:
        parser.error("pass at least one --source")

    checked, leaked = differential(arguments.source, arguments.show)
    print(f"relocations={checked} misfiled={leaked}")
    return 1 if leaked else 0


if __name__ == "__main__":
    raise SystemExit(main())
