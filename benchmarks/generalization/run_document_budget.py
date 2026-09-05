# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""What does the document know and not show, and can a reader get to it?

Every panel truncates twice. The panel keeps at most its own limit, and the
markdown then prints at most twenty-five rows of what survived. Both are
deliberate: a specification that printed every row would be an inventory, and
the projections exist to carry the inventory instead.

The question this asks is whether that split holds. A withheld row is fine
when a reader can reach it and knows where; it is a hole when the document
says it is somewhere it is not. `java.util.concurrent` was the first case
measured: 254 numeric tunables in the ledger, 120 in `spec.json`, 25 printed,
and a note under the table saying the other 95 are carried in `spec.json` and
`spec.index.json` -- which carries symbols without their metadata, so it holds
none of them.

Three numbers per panel, and the third is the one that matters:

1. printed -- rows in `spec.md`.
2. projected -- rows in `spec.json`, which the note points a reader to.
3. dropped -- rows the panel had and did not keep, so `spec.json` never saw
   them either.

A dropped row is not automatically lost. `spec.index.json` carries every
symbol's identity -- `qualified_name`, `kind`, `path`, `start_line` -- so a
panel made of symbol identity keeps its content reachable there. What the
index does not carry is `metadata`, and a panel built from metadata loses its
dropped rows completely. Rather than list which panels are which, which would
go stale the first time a panel is added, this tests each one: it asks whether
the subject of a row the panel kept is a name the index carries, since the
dropped rows come from the same generator as the kept ones.

It also reports how much of the repository a printed panel represents, since
rows are ordered by path and a table of twenty-five may be twenty-five rows
about the four files whose names sort first. That is not a defect on its own
-- some order has to be chosen -- but it decides whether a reader who trusts
the table is reading the repository or its first few files.

    python benchmarks/generalization/run_document_budget.py --source .

Exit status is zero. This measures what the document withholds; it is not a
gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository
from open_skeleton.spec.profile import load_profile
from open_skeleton.spec.render import MAX_PANEL_ROWS, build_spec


# The column a row cites its source in is the last one for every panel that
# has one. A panel without a citation column is counted as covering nothing,
# rather than guessed at.
def _cited_paths(rows: tuple[tuple[str, ...], ...]) -> set[str]:
    """Files the given rows cite, read from whichever cell carries a path."""

    found: set[str] = set()
    for row in rows:
        for cell in reversed(row):
            text = str(cell).strip("` ")
            if ":" in text and "/" in text.split(":")[0]:
                found.add(text.split(":")[0])
                break
            if "/" in text and "." in text.rsplit("/", 1)[-1]:
                found.add(text)
                break
    return found


def examine(repository: Path) -> list[dict[str, object]]:
    """One row per panel that withheld anything, for one repository."""

    with TemporaryDirectory() as temporary:
        ledger = EvidenceLedger(Path(temporary) / "evidence.sqlite3")
        ledger.initialize()
        snapshot = scan_repository(repository)
        ledger.save_snapshot(snapshot)
        result = analyze_snapshot(snapshot)
        ledger.save_analysis(result)
        document = build_spec(ledger, load_profile(None))

    # What `spec.index.json` carries: a symbol's identity and nothing from its
    # metadata. A row whose subject is one of these names survives the panel's
    # cap somewhere a reader can reach.
    indexed: set[str] = set()
    for symbol in result.symbols:
        name = symbol.qualified_name
        indexed.add(name)
        indexed.add(name.rsplit(".", 1)[-1])
        indexed.add(name.rsplit("::", 1)[-1])

    # Panels live in sections, and a panel of the same name can appear in more
    # than one. Each occurrence is truncated on its own, so each is counted.
    found: list[dict[str, object]] = []
    for panel in (item for section in document.sections for item in section.panels):
        printed = min(len(panel.rows), MAX_PANEL_ROWS)
        projected = len(panel.rows)
        unreachable = panel.dropped_rows
        if projected <= printed and not unreachable:
            continue
        everywhere = _cited_paths(panel.rows)
        shown = _cited_paths(panel.rows[:MAX_PANEL_ROWS])
        found.append(
            {
                "panel": panel.name,
                "printed": printed,
                "projected": projected,
                "dropped": unreachable,
                "indexed": _subject_is_indexed(panel.rows, indexed),
                "files_shown": len(shown),
                "files_held": len(everywhere),
            }
        )
    return found


def _subject_is_indexed(rows: tuple[tuple[str, ...], ...], indexed: set[str]) -> bool:
    """Whether this panel's rows are about names `spec.index.json` carries.

    Sampled from the rows the panel kept, because the dropped ones are gone
    and came from the same generator. A panel is called indexed when most of
    its subjects are symbol names: a couple of coincidental matches -- a
    constant that happens to share a function's name -- should not make a
    metadata panel look recoverable.
    """

    sample = [row[0].strip("`") for row in rows[:40] if row]
    if not sample:
        return False
    return sum(item in indexed for item in sample) * 2 > len(sample)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        action="append",
        type=Path,
        help="Repository to examine. Repeatable; defaults to this one.",
    )
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()
    sources = arguments.source or [Path.cwd()]

    report: dict[str, list[dict[str, object]]] = {}
    for source in sources:
        if not source.exists():
            print(f"missing: {source}")
            continue
        report[source.name or str(source)] = examine(source)

    if arguments.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    lost_total = 0
    dropped_total = 0
    for name, panels in report.items():
        print(f"\n## {name}\n")
        if not panels:
            print("  every panel prints everything it holds.")
            continue
        header = (
            f"  {'panel':28} {'printed':>8} {'in spec.json':>13} "
            f"{'dropped':>8} {'in the index':>13}  files"
        )
        print(header)
        print("  " + "-" * (len(header) - 2))
        for panel in sorted(panels, key=lambda item: -int(item["dropped"])):
            dropped = int(panel["dropped"])
            dropped_total += dropped
            if not panel["indexed"]:
                lost_total += dropped
            coverage = (
                f"{panel['files_shown']}/{panel['files_held']}" if panel["files_held"] else "--"
            )
            print(
                f"  {str(panel['panel'])[:28]:28} {panel['printed']:>8} "
                f"{panel['projected']:>13} {dropped:>8} "
                f"{('yes' if panel['indexed'] else 'no'):>13}  {coverage}"
            )

    print(
        f"\n{dropped_total:,} row(s) were dropped by a panel, of which {lost_total:,} are in "
        "no projection at all: `spec.index.json` carries a symbol's identity and nothing "
        "from its metadata, so a metadata panel's dropped rows are recoverable only by "
        "querying the ledger. A withheld row is fine when a reader can reach it and is told "
        "where; it is a hole when the document says it is somewhere it is not.\n"
    )
    print(
        "The files column is how many of the files a panel holds rows about are represented "
        "in the twenty-five it prints. Rows are ordered by path, so a low ratio means the "
        "printed table describes the repository's first few files rather than the "
        "repository.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
