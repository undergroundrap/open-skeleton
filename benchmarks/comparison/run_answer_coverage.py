# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Is the answer recorded at all, or merely hard to find?

`run_retrieval_cost.py` classifies a miss by what a query returned, which
cannot separate a fact nothing indexed from a fact nothing recorded. Those need
opposite work -- a better query against a reader that already saw it, or a
reader that never did -- and three sessions were spent tuning retrieval before
this distinction existed to be measured.

So this ignores retrieval entirely. It scans every claim and every declared
value the ledger holds, applies the same proximity rule the other scorers use,
and reports which questions are answerable from the ledger's contents. Set
against the retrieval score, the two numbers say where the bottleneck is:

  in the ledger, answered by query   retrieval and reading are both working
  in the ledger, missed by query     a retrieval problem
  not in the ledger                  a reading problem, and no query will fix it

    python benchmarks/comparison/run_answer_coverage.py \\
        --questions benchmarks/comparison/questions-urllib3.json \\
        --state-dir <dir>

Symbols are paged rather than capped. A first version stopped at 5,000 and zod
holds 11,043, so the module whose patterns answer four of its questions was
never scanned, and the report said those facts were unrecorded when they were
merely unread by this script.

Exit status is zero. This measures where to work; it is not a gate.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.ledger import EvidenceLedger, _declared_text
from open_skeleton.spec.render import _every_symbol

_COST_PATH = Path(__file__).resolve().parent / "run_retrieval_cost.py"
_spec = importlib.util.spec_from_file_location("retrieval_cost", _COST_PATH)
assert _spec is not None and _spec.loader is not None
_cost = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cost)


def records_for(ledger: EvidenceLedger, snapshot_id: str) -> list[str]:
    """Everything the ledger states, one body of text per record."""

    found: list[str] = [str(item["claim"]) for item in ledger.list_claims(snapshot_id, limit=5000)]
    for symbol in _every_symbol(ledger, snapshot_id):
        metadata: Any = symbol.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        declared = _declared_text(metadata)
        if declared:
            found.append(f"{symbol['qualified_name']} ({symbol['path']})\n{declared}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--show", type=int, default=12)
    arguments = parser.parse_args()

    payload = json.loads(arguments.questions.read_text(encoding="utf-8"))
    ledger = EvidenceLedger(arguments.state_dir / "evidence.sqlite3")
    latest = ledger.latest_snapshot()
    if latest is None:
        print("no snapshot in that state directory")
        return 0

    records = records_for(ledger, str(latest["snapshot_id"]))
    present: list[str] = []
    absent: list[str] = []
    for question in payload["questions"]:
        where = present if any(_cost.answered_by(item, question) for item in records) else absent
        where.append(str(question["id"]))

    total = len(payload["questions"])
    print(f"\n## Answer coverage on {payload.get('fixture', arguments.state_dir.name)}\n")
    print(f"records scanned      {len(records):,}")
    print(f"in the ledger        {len(present)}/{total}")
    if absent:
        print("\nrecorded nowhere, so no query will reach them:")
        for identifier in absent[: arguments.show]:
            question = next(item for item in payload["questions"] if item["id"] == identifier)
            print(f"  {identifier}  {question['question']}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
