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
import json
import os
import shlex
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


def _split_command(text: str) -> list[str]:
    """Split a command string into words without handing it to a shell.

    Neither `shlex` mode is right on its own on Windows. POSIX mode treats a
    backslash as an escape, so a native path loses every separator it has.
    Non-POSIX mode keeps the backslashes -- and also keeps the quote
    characters *inside* the token, so a quoted path containing a space
    reaches `subprocess` with its quotes attached and cannot be found.

    So: split without escape handling, then strip the quotes it split on.
    """

    words = shlex.split(text, posix=os.name != "nt")
    if os.name != "nt":
        return words
    return [
        word[1:-1] if len(word) >= 2 and word[0] == word[-1] and word[0] in "\"'" else word
        for word in words
    ]


def _usable_indexes(paths: list[Path]) -> bool:
    """Whether the declared indexes exist and parse as a semantic graph.

    A generator's exit code is not a verdict on its output. `hum graph` exits
    non-zero whenever the sources carry an error, and a compiler's own test
    corpus carries them on purpose -- hum-lang exits 1 while writing a
    complete 2.26 MB graph of 229 files. Failing the gate on that status
    rejected every turn on the repository this gate was built for, and the
    reason would have read as a broken generator rather than as a working one
    reporting the errors it was asked to find.

    So the status is a signal and the output is the evidence. Nothing here
    judges *quality*: a file that parses and names the schema is enough to say
    the run produced something to read.
    """

    if not paths:
        # Nothing was declared, so there is nothing to inspect and the exit
        # code is the only information available.
        return False
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(document, dict) or not str(document.get("schema", "")).startswith(
            "hum.semantic_graph"
        ):
            return False
    return True


def run(
    repository: Path,
    state: Path,
    hum_index: list[Path],
    required: list[str],
    fast: bool,
    minimum_coverage: float,
    minimum_yield: float,
) -> int:
    snapshot = scan_repository(repository)
    result = analyze_snapshot(snapshot, hum_index=hum_index or None)

    ledger = EvidenceLedger(state / "evidence.sqlite3")
    ledger.save_snapshot(snapshot)
    ledger.save_analysis(result)

    problems = 0

    # A language the scanner found and no analyzer read is a silent hole, and
    # a gate that stays green through one is worse than no gate.
    # Coverage is a ratio, not a boolean. Requiring only that *something*
    # was read let a stale index covering one file of 229 pass green while
    # 99.6% of the language went unexamined -- which is the shape a
    # regenerated index takes when part of it fails, not a hypothetical.
    unread = [
        (
            item.language,
            (
                f"{item.language}: {item.analyzed_files:,} of {item.eligible_files:,} "
                f"file(s) read ({item.coverage_ratio:.0%})"
            ),
        )
        for item in result.coverage
        if item.eligible_files and item.coverage_ratio < minimum_coverage
    ]
    # Coverage and yield answer different questions, and only the second one
    # is about substance. Wiring a real Hum index took that language from
    # 0/229 files read to 229/229 -- and its yield stayed at 0%, because the
    # index contributes structure rather than claims. A gate watching only
    # coverage would have called that success.
    silent = [
        (
            item.language,
            (
                f"{item.language}: {item.analyzed_files:,} file(s) read, "
                f"{(item.yield_ratio or 0.0):.0%} produced a finding"
            ),
        )
        for item in result.coverage
        if item.analyzed_files and (item.yield_ratio or 0.0) < minimum_yield
    ]
    blocking = [line for language, line in unread if language in required]
    blocking += [line for language, line in silent if language in required]
    if blocking:
        _emit("REQUIRED LANGUAGE BELOW THRESHOLD:", blocking)
        problems += 1
    elif unread or silent:
        _emit(
            "thin coverage (not required, reported once):",
            [line for _, line in unread] + [line for _, line in silent],
        )

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
        # One quoted string, split the way a shell would but never handed to
        # one. As a list of words (`nargs="+"`) argparse stopped at the first
        # token beginning with `-`, so the example this help text used to give
        # -- `hum graph --out build/graph.json` -- was rejected as unrecognized
        # arguments. Every realistic generator invocation takes a flag, so the
        # regeneration path could not be used as documented.
        help=(
            "Command that regenerates the Hum index before gating, as one "
            'quoted string: --hum-graph-command "hum graph src --out build/graph.json"'
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
        help="Fail when this language reads below --min-coverage. Repeatable.",
    )
    parser.add_argument(
        "--min-yield",
        type=float,
        default=0.0,
        help=(
            "Share of a required language's read files that must produce at "
            "least one claim. Defaults to 0: reading a file and having nothing "
            "to say about it is a real answer for some languages, and a gate "
            "should not invent a finding to satisfy a threshold."
        ),
    )
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=0.95,
        help=(
            "Share of a required language's eligible files that must be read "
            "(default 0.95). A partial index is the usual cause."
        ),
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
        command = _split_command(arguments.hum_graph_command)
        if not command:
            print("HUM INDEX — --hum-graph-command was empty.")
            return 1
        try:
            completed = subprocess.run(  # noqa: S603
                command, cwd=repository, capture_output=True, text=True, check=False
            )
        except OSError as exc:
            # A generator that is not installed is not a verdict on the work.
            # Exiting 2 keeps "ask me again" distinct from "this change is
            # bad", which is the whole point of this script's exit codes.
            print(f"HUM INDEX — could not run `{command[0]}`: {exc.__class__.__name__}: {exc}")
            return 2
        if completed.returncode and not _usable_indexes(arguments.hum_index):
            print("HUM INDEX — the generator failed, so Hum sources will read as nothing:")
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            print(f"  {detail[-1][:200] if detail else 'no output'}")
            return 1

    try:
        return run(
            repository,
            state,
            arguments.hum_index,
            arguments.require_language,
            arguments.fast,
            arguments.min_coverage,
            arguments.min_yield,
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
