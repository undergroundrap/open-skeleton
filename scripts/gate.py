# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Run the same checks CI runs, locally, on this machine's Python.

CI minutes are metered on a private repository, and a check that only ever
runs after a push is a check that does not help while the work is being done.
This runs every gate the workflow runs so a failure is found before it costs
anything, and so the workflow becomes confirmation rather than discovery.

What it cannot do is stand in for the operating-system matrix. CI runs Ubuntu
and Windows; this runs whatever you are on, and a path-separator or
line-ending fault will not appear here. That gap is reported at the end
rather than left implied.

    python scripts/gate.py            # everything that does not need a network
    python scripts/gate.py --full     # adds the dependency audit and a build
    python scripts/gate.py --fix      # formats and autofixes, then verifies
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ("src", "tests", "benchmarks")


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    command: tuple[str, ...]
    # A check needing the network is skipped by default so an offline run is
    # still a clean run rather than a misleading failure.
    needs_network: bool = False


def _checks(fix: bool) -> tuple[Check, ...]:
    python = sys.executable
    formatter = ("ruff", "format") if fix else ("ruff", "format", "--check")
    linter = ("ruff", "check", "--fix") if fix else ("ruff", "check")
    return (
        Check("compile", (python, "-m", "compileall", "-q", "src", "tests")),
        Check("docs", (python, "scripts/check_docs.py")),
        Check("format", (python, "-m", *formatter, *TARGETS)),
        Check("lint", (python, "-m", *linter, *TARGETS)),
        Check("types", (python, "-m", "mypy")),
        Check("tests", (python, "-m", "unittest", "discover", "-s", "tests")),
        Check("audit", (python, "-m", "pip_audit", "--local"), needs_network=True),
        Check("build", (python, "-m", "build"), needs_network=True),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Also run the checks that need a network: dependency audit and build.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply formatting and lint autofixes instead of only reporting them.",
    )
    args = parser.parse_args()

    # CI installs the package before running anything. Without that, every
    # import fails and the output is a wall of ModuleNotFoundError that says
    # nothing about the code — so check once and say what to do about it.
    probe = subprocess.run(
        (sys.executable, "-c", "import open_skeleton"),
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if probe.returncode != 0:
        print(
            "open_skeleton is not importable by this interpreter, so the gate "
            "would report import errors rather than results.\n\n"
            f'    {Path(sys.executable).name} -m pip install -e ".[dev,mcp]"\n\n'
            f"Interpreter: {sys.executable}"
        )
        return 1

    failures: list[str] = []
    skipped: list[str] = []
    started = time.monotonic()

    for check in _checks(args.fix):
        if check.needs_network and not args.full:
            skipped.append(check.name)
            continue
        if shutil.which(check.command[0]) is None and check.command[0] != sys.executable:
            skipped.append(check.name)
            continue
        # ASCII only: a Windows console defaults to cp1252 and raises on box
        # drawing, which would fail the runner rather than the code it checks.
        print(f"\n--- {check.name} " + "-" * max(0, 60 - len(check.name)))
        completed = subprocess.run(check.command, cwd=ROOT, check=False)  # noqa: S603
        if completed.returncode != 0:
            failures.append(check.name)

    elapsed = time.monotonic() - started
    print("\n" + "=" * 68)
    if failures:
        print(f"FAILED: {', '.join(failures)}  ({elapsed:.1f}s)")
    else:
        print(f"All gates passed in {elapsed:.1f}s.")
    if skipped:
        print(f"Skipped (needs --full or a missing tool): {', '.join(skipped)}")
    print(
        f"Verified on {platform.system()} {platform.machine()} with Python "
        f"{platform.python_version()}. CI additionally runs the other operating "
        "system in its matrix, which this cannot substitute for."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
