# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Run the whole pipeline over many repositories and report what broke.

`run_robustness.py` sweeps for crashes and silence at the *analysis* level.
This goes one stage further and renders a specification for each repository,
then asks whether that document is defensible: does it agree with itself, and
does it account for everything the ledger holds.

Both questions needed a sweep rather than a fixture suite, because both
classes of defect are invisible in any single repository.

The first sweep of 58 packages found a crash rather than an incoherence:
`sympy/polys/numberfields/resolvent_lookup.py` is one arithmetic expression
nested about four hundred nodes deep, `ast.NodeVisitor` recurses per node, and
the handler covering parse did not cover the walk -- so a 2,600-file
repository produced no analysis at all.

The second class needed size. A specification builder read one page of claims
and reported the page size as the ledger's contents; nothing smaller than
`java.base` ever exceeded a page, so the defect sat behind every corpus until
one arrived with 8,707 claims.

    python benchmarks/robustness/run_document_sweep.py --root .venv/Lib/site-packages
    python benchmarks/robustness/run_document_sweep.py --repo path/one --repo path/two

Exit status is non-zero when anything crashed or any document disagreed with
itself, so this is usable as a gate over a corpus a team already trusts.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository
from open_skeleton.spec import (
    build_spec,
    load_profile,
    render_spec_markdown,
)
from open_skeleton.spec.coherence import check_coherence, check_conservation

# Directories that are packaging artifacts rather than repositories.
SKIP_SUFFIXES = (".dist-info", ".egg-info", ".egg")


def targets(root: Path | None, repos: list[Path]) -> list[Path]:
    found = list(repos)
    if root is not None:
        found += sorted(
            item
            for item in root.iterdir()
            if item.is_dir()
            and item.name != "__pycache__"
            and not item.name.endswith(SKIP_SUFFIXES)
        )
    return found


def _undeliverable(document: Any, markdown: str) -> tuple[str, ...]:
    """Whether the document that was built could actually be written out.

    This sweep proved a specification agreed with itself and never proved it
    could be delivered, so `pygments` passed it while producing an empty
    `spec.json`: the Unicode category of surrogates is spelled with
    surrogates, which UTF-8 cannot encode. Every file was read correctly and
    the run still ended with nothing, and a coherence check on a document
    nobody can save is a check on the wrong thing.
    """

    findings: list[str] = []
    for label, text in (
        ("spec.md", markdown),
        ("spec.json", json.dumps(document.to_dict(), ensure_ascii=False)),
    ):
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as exc:
            findings.append(f"{label} cannot be written as UTF-8: {exc.reason} at {exc.start}")
    return tuple(findings)


def sweep(paths: list[Path], state: Path, show: int) -> tuple[int, int, int]:
    analyzed = incoherent = crashed = 0
    for index, target in enumerate(paths):
        try:
            snapshot = scan_repository(target)
            if not snapshot.files:
                continue
            ledger = EvidenceLedger(state / f"{index}.sqlite3")
            ledger.save_snapshot(snapshot)
            ledger.save_analysis(analyze_snapshot(snapshot))
            document = build_spec(ledger, load_profile())
            markdown = render_spec_markdown(document)
            findings = (
                check_coherence(document, markdown)
                + check_conservation(
                    document,
                    {
                        kind: ledger.count_rows(document.snapshot_id, kind)
                        for kind in ("claims", "symbols", "edges")
                    },
                )
                + _undeliverable(document, markdown)
            )
            analyzed += 1
            if findings:
                incoherent += 1
                print(f"INCOHERENT {target.name}", flush=True)
                for item in findings[:show]:
                    print(f"   {item}", flush=True)
        except Exception:  # noqa: BLE001 - a sweep reports failures, it does not raise
            crashed += 1
            print(f"CRASH {target.name}", flush=True)
            print("   " + traceback.format_exc().strip().splitlines()[-1], flush=True)
    return analyzed, incoherent, crashed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Directory whose children are repositories.")
    parser.add_argument(
        "--repo", type=Path, action="append", default=[], help="One repository; repeatable."
    )
    parser.add_argument("--show", type=int, default=4, help="Findings printed per repository.")
    arguments = parser.parse_args()

    paths = targets(arguments.root, arguments.repo)
    if not paths:
        parser.error("pass --root or at least one --repo")

    with tempfile.TemporaryDirectory() as scratch:
        analyzed, incoherent, crashed = sweep(paths, Path(scratch), arguments.show)

    print(f"repositories={analyzed} incoherent={incoherent} crashed={crashed}")
    return 1 if (incoherent or crashed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
