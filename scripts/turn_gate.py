# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""A per-turn evidence gate: silent when clean, specific when not.

Meant to run on every turn of an agent loop, as a hook rather than as
context. It analyzes the repository, audits the claim ledger for the shapes
past mistakes took, and renders the specification to check that the document
agrees with itself and accounts for everything the ledger holds.

Printing nothing on success is the whole design. A check that speaks every
turn becomes wallpaper: this project's own Rust differential labelled its
output badly, and nineteen real defects sat unread beneath a hundred and
seventy-four expected ones until someone went looking. Output that is always
there is output nobody reads, and that is worse than no check, because it
also carries the authority of having run.

The exit code is the interface. `0` means every gate passed, `1` means one
found something and the reason is on stdout, bounded to a few lines so a loop
can paste it into a turn without paying for a specification. `2` means the
gate could not run at all, which is not a verdict on the work: a loop that
treats it as one rejects good changes whenever the ledger is busy.

    python scripts/turn_gate.py --repo C:\\path\\to\\repository
    python scripts/turn_gate.py --repo . --hum-index build/graph.json

`--hum-index` matters for any repository holding Hum sources. This tool does
not run the target compiler, so without a pre-generated
`hum.semantic_graph.v0` those files are counted as eligible and read as
nothing: hum-lang reports 70 Rust files at 92.9% yield and 229 Hum files at
0%. A gate that is green while blind to most of a repository is the exact
failure this script exists to avoid, so `--require-language` makes that
condition fail loudly instead.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from open_skeleton.analysis import analyze_snapshot  # noqa: E402
from open_skeleton.audit import audit_claims  # noqa: E402
from open_skeleton.ledger import EvidenceLedger  # noqa: E402
from open_skeleton.scanner import scan_repository  # noqa: E402
from open_skeleton.spec import (  # noqa: E402
    build_spec,
    load_profile,
    render_spec_markdown,
    verify_spec,
)
from open_skeleton.spec.coherence import check_coherence, check_conservation  # noqa: E402
from open_skeleton.state import resolve_state_dir  # noqa: E402

MAX_LINES = 12


def _emit(header: str, lines: list[str]) -> None:
    print(header)
    for line in lines[:MAX_LINES]:
        print(f"  {line}")
    if len(lines) > MAX_LINES:
        print(f"  ... and {len(lines) - MAX_LINES:,} more")


def run(
    repository: Path,
    state: Path,
    hum_index: list[Path],
    required: list[str],
    fast: bool,
) -> int:
    snapshot = scan_repository(repository)
    result = analyze_snapshot(snapshot, hum_index=hum_index or None)

    ledger = EvidenceLedger(state / "evidence.sqlite3")
    ledger.save_snapshot(snapshot)
    ledger.save_analysis(result)

    problems = 0

    # A language the scanner found and no analyzer read is a silent hole, and
    # a gate that stays green through one is worse than no gate.
    unread = [
        f"{item.language}: {item.eligible_files:,} file(s) eligible, {item.analyzed_files:,} read"
        for item in result.coverage
        if item.eligible_files and not item.analyzed_files
    ]
    blocking = [line for line in unread if line.split(":")[0] in required]
    if blocking:
        _emit("UNREAD LANGUAGE — required by --require-language:", blocking)
        problems += 1
    elif unread:
        _emit("unread language (not required, reported once):", unread)

    findings = audit_claims(
        tuple(item.to_dict() for item in result.claims),
        tuple(item.to_dict() for item in result.evidence),
        tuple(item.to_dict() for item in snapshot.files),
    )
    if findings:
        _emit(
            "AUDIT — claim groups shaped like a past mistake:",
            [f"[{item.check}] {item.category}: {item.detail}" for item in findings],
        )
        problems += 1

    # Rendering the specification costs more than everything else here
    # combined -- on a 70-file crate: 3.5s to analyze and 26s to build the
    # document. A per-turn hook that takes half a minute gets removed, so
    # `--fast` keeps the claim-level checks and defers the document-level
    # ones to whatever runs before work is accepted.
    if fast:
        return 1 if problems else 0

    document = build_spec(ledger, load_profile())
    markdown = render_spec_markdown(document)
    incoherences = check_coherence(document, markdown) + check_conservation(
        document,
        {
            kind: ledger.count_rows(document.snapshot_id, kind)
            for kind in ("claims", "symbols", "edges")
        },
    )
    if incoherences:
        _emit("DOCUMENT — statements that disagree with the data:", [str(i) for i in incoherences])
        problems += 1

    report = verify_spec(document, ledger, root=repository)
    if report.failures:
        _emit(
            f"CITATIONS — {len(report.failures):,} of {report.total:,} do not resolve:",
            [f"{item.evidence_id[:8]} {item.status}" for item in report.failures],
        )
        problems += 1

    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(), help="Repository to gate.")
    parser.add_argument("--state-dir", type=Path, help="Where the ledger lives.")
    parser.add_argument(
        "--hum-index",
        type=Path,
        action="append",
        default=[],
        help="Pre-generated hum.semantic_graph.v0 JSON; repeat to combine shards.",
    )
    parser.add_argument(
        "--hum-graph-command",
        nargs="+",
        help=(
            "Command that regenerates the Hum index before gating, "
            "for example: --hum-graph-command hum graph --out build/graph.json"
        ),
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Skip the document checks, which cost about nine tenths of the "
            "runtime. Keeps analysis, the audit, and the unread-language check."
        ),
    )
    parser.add_argument(
        "--require-language",
        action="append",
        default=[],
        help="Fail when this language is eligible and unread. Repeatable.",
    )
    arguments = parser.parse_args()

    repository = arguments.repo.expanduser().resolve(strict=True)
    # A per-repository location, resolved the same way the CLI resolves it.
    # A single shared directory under the parent looked tidier and made every
    # repository in a workspace contend for one SQLite file, which surfaces as
    # `database is locked` -- an infrastructure failure wearing the costume of
    # a rejected change.
    state = resolve_state_dir(repository, arguments.state_dir)
    state.mkdir(parents=True, exist_ok=True)

    if arguments.hum_graph_command:
        # Regenerating the index is the caller's decision, never implicit:
        # this tool does not run a target compiler on its own.
        completed = subprocess.run(  # noqa: S603
            arguments.hum_graph_command, cwd=repository, capture_output=True, text=True, check=False
        )
        if completed.returncode:
            print("HUM INDEX — the generator failed, so Hum sources will read as nothing:")
            print(f"  {(completed.stderr or completed.stdout).strip().splitlines()[-1][:200]}")
            return 1

    try:
        return run(
            repository,
            state,
            arguments.hum_index,
            arguments.require_language,
            arguments.fast,
        )
    except (OSError, sqlite3.Error) as exc:
        # A gate that cannot run has not judged the work. Saying so with a
        # distinct status is the difference between "this change is bad" and
        # "ask me again"; a loop that conflates them rejects good work
        # whenever the ledger is busy, and a traceback in a hook is noise
        # nobody reads.
        print(f"GATE DID NOT RUN — {exc.__class__.__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
