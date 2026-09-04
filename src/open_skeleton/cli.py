# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from open_skeleton import __version__
from open_skeleton.analysis import analyze_snapshot
from open_skeleton.audit import audit_claims
from open_skeleton.benchmark import run_benchmark
from open_skeleton.dashboard import serve_dashboard
from open_skeleton.exports import (
    export_analysis_jsonl,
    export_analysis_markdown,
    export_jsonl,
    export_markdown,
)
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.models import ScanEvent
from open_skeleton.policy import ScanPolicy
from open_skeleton.providers import (
    ClaudeCliProvider,
    CodexCliProvider,
    DisabledProvider,
    LocalCommandProvider,
    ProviderAdapter,
    ProviderRequest,
)
from open_skeleton.scanner import scan_repository
from open_skeleton.spec import (
    build_spec,
    every_claim,
    load_profile,
    render_spec_index_json,
    render_spec_json,
    render_spec_markdown,
    verify_spec,
)
from open_skeleton.spec.coherence import check_coherence, check_conservation
from open_skeleton.spec.concordance import (
    build_record_concordance,
    build_value_set_concordance,
)
from open_skeleton.spec.render import _every_symbol, _languages_no_analyzer_read
from open_skeleton.state import resolve_state_dir
from open_skeleton.synthesis_assembly import assemble_synthesis
from open_skeleton.synthesis_plan import build_synthesis_plan
from open_skeleton.synthesis_runner import run_synthesis_plan, validate_external_output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="open-skeleton",
        description="Local-first, evidence-first codebase intelligence.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser(
        "scan",
        help="Create a deterministic, read-only repository inventory.",
    )
    scan.add_argument("path", nargs="?", default=".", help="Repository directory to scan.")
    scan.add_argument(
        "--state-dir",
        type=Path,
        help="Output directory. Defaults to the OS-local state area for PATH.",
    )
    scan.add_argument(
        "--max-file-bytes",
        type=int,
        default=2_000_000,
        help="Maximum bytes read from one file (default: 2,000,000).",
    )
    scan.add_argument("--quiet", action="store_true", help="Suppress progress events.")
    scan.add_argument(
        "--json",
        action="store_true",
        help="Print the final summary as JSON.",
    )

    analyze = subparsers.add_parser(
        "analyze",
        help="Scan and run deterministic semantic analyzers.",
    )
    analyze.add_argument("path", nargs="?", default=".", help="Repository directory to analyze.")
    analyze.add_argument(
        "--state-dir",
        type=Path,
        help="Output directory. Defaults to the OS-local state area for PATH.",
    )
    analyze.add_argument(
        "--max-file-bytes",
        type=int,
        default=2_000_000,
        help="Maximum bytes read from one file (default: 2,000,000).",
    )
    analyze.add_argument("--quiet", action="store_true", help="Suppress progress events.")
    analyze.add_argument("--json", action="store_true", help="Print the analysis summary as JSON.")
    analyze.add_argument(
        "--hum-index",
        type=Path,
        action="append",
        help=(
            "Pre-generated hum.semantic_graph.v0 JSON; the target compiler is never run "
            "implicitly. `hum graph` accepts multiple paths and merges them. Repeat this "
            "flag to combine sharded indexes."
        ),
    )

    status = subparsers.add_parser("status", help="Show the latest stored snapshot.")
    status.add_argument("path", nargs="?", default=".", help="Previously scanned repository.")
    status.add_argument(
        "--state-dir",
        type=Path,
        help="State directory. Defaults to the OS-local state area for PATH.",
    )
    status.add_argument("--json", action="store_true", help="Print status as JSON.")

    coverage = subparsers.add_parser(
        "coverage",
        help="Show what this analysis read and what it did not, so an absence can be judged.",
    )
    coverage.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    coverage.add_argument("--state-dir", type=Path, help="State directory.")
    coverage.add_argument("--snapshot", help="Snapshot ID. Defaults to the latest snapshot.")
    coverage.add_argument("--limit", type=int, default=8, help="Exclusion reasons to name.")
    coverage.add_argument("--json", action="store_true", help="Print the complete report as JSON.")

    refusals = subparsers.add_parser(
        "refusals",
        help=(
            "Show what each symbol refuses with, and the message it gives. "
            "Read from the guard-and-exit trace, which covers route handlers "
            "and functions with at least two guards."
        ),
    )
    refusals.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    refusals.add_argument("--state-dir", type=Path, help="State directory.")
    refusals.add_argument("--snapshot", help="Snapshot ID. Defaults to the latest snapshot.")
    refusals.add_argument(
        "--term", help="Only symbols, paths, statuses, or messages containing this text."
    )
    refusals.add_argument(
        "--with-message",
        action="store_true",
        help="Only refusals carrying a literal message.",
    )
    refusals.add_argument("--limit", type=int, default=40)
    refusals.add_argument("--json", action="store_true", help="Print complete rows as JSON.")

    contracts = subparsers.add_parser(
        "contracts",
        help="Show contracts declared in more than one form, and where each is written.",
    )
    contracts.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    contracts.add_argument("--state-dir", type=Path, help="State directory.")
    contracts.add_argument("--snapshot", help="Snapshot ID. Defaults to the latest snapshot.")
    contracts.add_argument(
        "--term",
        help="Only contracts whose members, fields, or labels contain this text.",
    )
    contracts.add_argument("--kind", choices=["all", "value-set", "record"], default="all")
    contracts.add_argument("--json", action="store_true", help="Print complete rows as JSON.")

    claims = subparsers.add_parser("claims", help="List claims from the latest analysis.")
    claims.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    claims.add_argument("--state-dir", type=Path, help="State directory.")
    claims.add_argument("--snapshot", help="Snapshot ID. Defaults to the latest snapshot.")
    claims.add_argument(
        "--status", choices=["verified", "inferred", "conflict", "unknown", "stale"]
    )
    claims.add_argument("--category")
    claims.add_argument("--limit", type=int, default=200)
    claims.add_argument("--json", action="store_true", help="Print complete claim objects as JSON.")

    audit = subparsers.add_parser(
        "audit",
        help="Flag claim groups shaped like a past mistake, on any repository.",
    )
    audit.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    audit.add_argument("--state-dir", type=Path, help="State directory.")
    audit.add_argument("--snapshot", help="Snapshot ID. Defaults to the latest snapshot.")
    audit.add_argument("--json", action="store_true", help="Print findings as JSON.")
    audit.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when any finding is reported.",
    )

    search = subparsers.add_parser("search", help="Search accepted and unresolved claims.")
    search.add_argument("query", help="FTS5 query or fallback substring.")
    search.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    search.add_argument("--state-dir", type=Path, help="State directory.")
    search.add_argument("--snapshot", help="Snapshot ID. Defaults to the latest snapshot.")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--json", action="store_true")

    evidence = subparsers.add_parser("evidence", help="Get one immutable evidence receipt.")
    evidence.add_argument("evidence_id")
    evidence.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    evidence.add_argument("--state-dir", type=Path, help="State directory.")

    diff = subparsers.add_parser("diff", help="Compare snapshots and project stale claims.")
    diff.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    diff.add_argument("--state-dir", type=Path, help="State directory.")
    diff.add_argument("--from-snapshot", dest="from_snapshot")
    diff.add_argument("--to-snapshot", dest="to_snapshot")
    diff.add_argument("--json", action="store_true")

    synthesize = subparsers.add_parser(
        "synthesize",
        help="Explicitly send a bounded context pack to an optional model provider.",
    )
    synthesize.add_argument("query", help="Claim search query used to build the context pack.")
    synthesize.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    synthesize.add_argument("--state-dir", type=Path, help="State directory.")
    synthesize.add_argument(
        "--provider",
        required=True,
        choices=["disabled", "codex", "claude", "local-command"],
    )
    synthesize.add_argument("--task", default="Synthesize the supplied evidence.")
    synthesize.add_argument("--model")
    synthesize.add_argument("--timeout-seconds", type=int, default=300)
    synthesize.add_argument("--max-chars", type=int, default=20_000)
    synthesize.add_argument("--max-claims", type=int, default=12)
    synthesize.add_argument(
        "--command",
        dest="provider_command",
        nargs="+",
        help="Executable and arguments for --provider local-command.",
    )
    synthesize.add_argument("--json", action="store_true")

    plan_synthesis = subparsers.add_parser(
        "plan-synthesis",
        help="Build source-grounded jobs for parallel narrative synthesis without running a model.",
    )
    plan_synthesis.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    plan_synthesis.add_argument("--state-dir", type=Path, help="State directory.")
    plan_synthesis.add_argument("--snapshot", help="Snapshot ID. Defaults to the latest snapshot.")
    plan_synthesis.add_argument(
        "--profile",
        type=Path,
        help="Outline profile JSON. Defaults to the packaged standard profile.",
    )
    plan_synthesis.add_argument(
        "--output",
        type=Path,
        help="Plan path. Defaults to <state-dir>/synthesis-plan.json.",
    )
    plan_synthesis.add_argument("--max-chars", type=int, default=20_000)
    plan_synthesis.add_argument("--max-claims", type=int, default=100)
    plan_synthesis.add_argument("--json", action="store_true")

    run_plan = subparsers.add_parser(
        "run-synthesis-plan",
        help="Dry-run or explicitly execute a bounded synthesis plan.",
    )
    run_plan.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    run_plan.add_argument("--state-dir", type=Path, help="State directory.")
    run_plan.add_argument(
        "--plan", type=Path, help="Plan path. Defaults to <state-dir>/synthesis-plan.json."
    )
    run_plan.add_argument(
        "--output-dir",
        type=Path,
        help="External result directory. Defaults to <state-dir>/synthesis-runs/<provider>.",
    )
    run_plan.add_argument("--provider", required=True, choices=["codex", "claude", "local-command"])
    run_plan.add_argument("--execute", action="store_true", help="Actually contact the provider.")
    run_plan.add_argument("--model")
    run_plan.add_argument("--timeout-seconds", type=int, default=300)
    run_plan.add_argument("--concurrency", type=int, default=1)
    run_plan.add_argument("--max-jobs", type=int, default=100)
    run_plan.add_argument(
        "--command",
        dest="provider_command",
        nargs="+",
        help="Executable and arguments for --provider local-command.",
    )
    run_plan.add_argument("--json", action="store_true")

    assemble = subparsers.add_parser(
        "assemble-synthesis",
        help="Validate completed synthesis receipts and render a separate Markdown projection.",
    )
    assemble.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    assemble.add_argument("--state-dir", type=Path, help="State directory.")
    assemble.add_argument(
        "--plan", type=Path, help="Plan path. Defaults to <state-dir>/synthesis-plan.json."
    )
    assemble.add_argument(
        "--results-dir",
        required=True,
        type=Path,
        help="External synthesis-run directory containing completed job receipts.",
    )
    assemble.add_argument(
        "--output",
        type=Path,
        help="Markdown path. Defaults to <state-dir>/source-grounded-synthesis.md.",
    )
    assemble.add_argument("--max-jobs", type=int, default=1_000)
    assemble.add_argument("--json", action="store_true")

    benchmark = subparsers.add_parser(
        "benchmark",
        help="Run a pinned, machine-readable accuracy and performance benchmark.",
    )
    benchmark.add_argument("path", help="Repository fixture directory.")
    benchmark.add_argument("--gold", required=True, type=Path, help="Gold benchmark JSON.")
    benchmark.add_argument("--output-dir", required=True, type=Path)
    benchmark.add_argument("--json", action="store_true")

    spec = subparsers.add_parser(
        "spec",
        help="Project the claim ledger through an outline profile into a specification.",
    )
    spec.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    spec.add_argument("--state-dir", type=Path, help="State directory.")
    spec.add_argument("--snapshot", help="Snapshot ID. Defaults to the latest snapshot.")
    spec.add_argument(
        "--profile",
        type=Path,
        help="Outline profile JSON. Defaults to the packaged standard profile.",
    )
    spec.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Where to write spec.md, spec.json and spec.index.json. "
            "Defaults to the state directory."
        ),
    )
    spec.add_argument(
        "--verify",
        action="store_true",
        help="Re-resolve every citation against current sources and report integrity.",
    )
    spec.add_argument("--json", action="store_true", help="Print the summary as JSON.")

    serve = subparsers.add_parser("serve", help="Run the read-only local findings dashboard.")
    serve.add_argument("path", nargs="?", default=".", help="Analyzed repository.")
    serve.add_argument("--state-dir", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    return parser


def _resolve_root(value: str) -> Path:
    return Path(value).expanduser().resolve(strict=True)


def _resolve_state_dir(root: Path, value: Path | None) -> Path:
    return resolve_state_dir(root, value)


def _scan(args: argparse.Namespace) -> int:
    if args.max_file_bytes < 1:
        raise ValueError("--max-file-bytes must be positive")

    root = _resolve_root(args.path)
    state_dir = _resolve_state_dir(root, args.state_dir)

    def show_event(event: ScanEvent) -> None:
        if not args.quiet:
            print(f"[{event.stage}] {event.message}", file=sys.stderr)

    snapshot = scan_repository(
        root,
        policy=ScanPolicy(max_file_bytes=args.max_file_bytes),
        on_event=show_event,
    )
    ledger_path = state_dir / "evidence.sqlite3"
    jsonl_path = state_dir / "inventory.jsonl"
    markdown_path = state_dir / "inventory.md"

    EvidenceLedger(ledger_path).save_snapshot(snapshot)
    export_jsonl(snapshot, jsonl_path)
    export_markdown(snapshot, markdown_path)

    summary = {
        **snapshot.summary(),
        "ledger": str(ledger_path),
        "jsonl": str(jsonl_path),
        "markdown": str(markdown_path),
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Snapshot: {snapshot.snapshot_id}")
        print(f"Included: {len(snapshot.files):,} files / {snapshot.total_lines:,} lines")
        print(f"Excluded: {len(snapshot.exclusions):,} entries")
        print(f"Ledger: {ledger_path}")
        print(f"Report: {markdown_path}")
    return 0


def _status(args: argparse.Namespace) -> int:
    root = _resolve_root(args.path)
    state_dir = _resolve_state_dir(root, args.state_dir)
    ledger = EvidenceLedger(state_dir / "evidence.sqlite3")
    latest = ledger.latest_snapshot()
    if latest is None:
        print(f"No snapshot found in {state_dir}", file=sys.stderr)
        return 1

    analysis = ledger.latest_analysis(latest["snapshot_id"])
    if analysis is not None:
        latest["analysis"] = analysis
    if args.json:
        print(json.dumps(latest, indent=2, sort_keys=True))
    else:
        print(f"Snapshot: {latest['snapshot_id']}")
        print(f"Root: {latest['root_path']}")
        print(f"Last seen: {latest['last_seen_at']}")
        print(f"Included: {latest['file_count']:,} files / {latest['total_lines']:,} lines")
        print(f"Excluded: {latest['excluded_count']:,} entries")
    return 0


def _analyze(args: argparse.Namespace) -> int:
    if args.max_file_bytes < 1:
        raise ValueError("--max-file-bytes must be positive")
    root = _resolve_root(args.path)
    state_dir = _resolve_state_dir(root, args.state_dir)

    def show_event(event: ScanEvent) -> None:
        if not args.quiet:
            print(f"[{event.stage}] {event.message}", file=sys.stderr)

    snapshot = scan_repository(
        root,
        policy=ScanPolicy(max_file_bytes=args.max_file_bytes),
        on_event=show_event,
    )
    if not args.quiet:
        print("[analyzing] Running deterministic semantic adapters", file=sys.stderr)
    result = analyze_snapshot(snapshot, hum_index=args.hum_index)
    ledger_path = state_dir / "evidence.sqlite3"
    ledger = EvidenceLedger(ledger_path)
    previous_snapshots = ledger.snapshots_for_root(root, limit=1)
    previous_snapshot_id = str(previous_snapshots[0]["snapshot_id"]) if previous_snapshots else None
    ledger.save_snapshot(snapshot)
    run_id = ledger.save_analysis(result)
    stale_claims: list[dict[str, object]] = []
    difference: dict[str, object] | None = None
    if previous_snapshot_id and previous_snapshot_id != snapshot.snapshot_id:
        difference = ledger.diff_snapshots(previous_snapshot_id, snapshot.snapshot_id)
        stale_claims = ledger.project_stale_claims(previous_snapshot_id, snapshot.snapshot_id)

    inventory_jsonl = state_dir / "inventory.jsonl"
    inventory_markdown = state_dir / "inventory.md"
    analysis_jsonl = state_dir / "analysis.jsonl"
    analysis_markdown = state_dir / "analysis.md"
    export_jsonl(snapshot, inventory_jsonl)
    export_markdown(snapshot, inventory_markdown)
    export_analysis_jsonl(result, analysis_jsonl)
    export_analysis_markdown(result, analysis_markdown)

    summary = {
        **result.summary(),
        "run_id": run_id,
        "root": str(root),
        "ledger": str(ledger_path),
        "analysis_jsonl": str(analysis_jsonl),
        "analysis_markdown": str(analysis_markdown),
        "diff": difference,
        "stale_claim_count": len(stale_claims),
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Snapshot: {snapshot.snapshot_id}")
        print(f"Symbols: {len(result.symbols):,}")
        print(f"Relationships: {len(result.edges):,}")
        print(f"Evidence receipts: {len(result.evidence):,}")
        print(f"Claims: {len(result.claims):,}")
        print(f"Report: {analysis_markdown}")
    return 0


def _snapshot_drift(root: Path, ledger: EvidenceLedger, snapshot_id: str) -> str | None:
    """How far the working tree has moved from the snapshot, or None if not at all.

    Every query here answers from a stored snapshot. Nothing checked that the
    snapshot still described the files on disk, so editing a repository and
    asking a question returned a confident answer about a state that no longer
    existed -- one file counted where two were present, and a cheerful report
    that everything had been read.

    That is the failure this whole project is built to prevent, arriving
    through its own front door. A stale answer is worse than no answer,
    because it looks like an answer.

    The check re-scans, which costs about as much as the query itself and is
    exact: the snapshot identifier is a digest of the file set, so equal
    identifiers mean the answer still describes the repository.
    """

    try:
        current = scan_repository(root)
    except (OSError, ValueError):
        # A repository that cannot be scanned cannot be compared. Say nothing
        # rather than claim freshness this did not establish.
        return None
    if current.snapshot_id == snapshot_id:
        return None

    stored = {str(item["path"]): int(item["size_bytes"]) for item in ledger.list_files(snapshot_id)}
    present = {item.path: item.size_bytes for item in current.files}
    added = sorted(set(present) - set(stored))
    removed = sorted(set(stored) - set(present))
    changed = sorted(path for path in set(stored) & set(present) if stored[path] != present[path])
    parts = [
        f"{len(added):,} added" if added else "",
        f"{len(removed):,} removed" if removed else "",
        f"{len(changed):,} changed" if changed else "",
    ]
    summary = ", ".join(part for part in parts if part)
    named = ", ".join((added + removed + changed)[:3])
    if not summary:
        # Same paths and sizes, different digest: an edit that kept the byte
        # count. Still stale, and saying "0 changed" would be a worse answer
        # than saying the contents moved.
        summary = "content changed"
        named = ""
    detail = f": {named}" if named else ""
    return (
        f"This answer is from a snapshot that no longer matches the working tree "
        f"({summary}{detail}). Re-run `open-skeleton analyze` to refresh it."
    )


def _ledger_and_snapshot(args: argparse.Namespace) -> tuple[EvidenceLedger, str]:
    root = _resolve_root(args.path)
    state_dir = _resolve_state_dir(root, args.state_dir)
    ledger = EvidenceLedger(state_dir / "evidence.sqlite3")
    if args.snapshot:
        return ledger, args.snapshot
    latest = ledger.latest_snapshot()
    if latest is None:
        # A dead end helps nobody. Name the command that produces what is
        # missing, because whoever hit this has not read the README.
        raise ValueError(
            f"No snapshot found in {state_dir}. "
            f"Run `open-skeleton analyze {root}` first; these commands answer "
            "from a stored analysis rather than reading the repository."
        )
    return ledger, str(latest["snapshot_id"])


def _contracts(args: argparse.Namespace) -> int:
    """Answer "what else moves if I change this?" without rendering a document.

    The concordances are the most expensive facts to recover by reading and
    the cheapest to answer once recovered, and until now they existed only
    inside `spec.json` -- a hundred thousand words a caller had to load in
    full to learn that a vocabulary is declared in five places. This reads
    the same ledger the document is projected from and prints the rows that
    match, which is the difference between a specification a team publishes
    and a specification an agent can ask.
    """

    ledger, snapshot_id = _ledger_and_snapshot(args)
    drift = _snapshot_drift(_resolve_root(args.path), ledger, snapshot_id)
    symbols = tuple(_every_symbol(ledger, snapshot_id))
    value_sets, ambiguous = build_value_set_concordance(snapshot_id=snapshot_id, symbols=symbols)
    records = build_record_concordance(snapshot_id=snapshot_id, symbols=symbols)

    term = (args.term or "").casefold()
    if term:
        value_sets = tuple(
            item
            for item in value_sets
            if any(term in member.casefold() for member in item.members)
            or any(term in entry.label.casefold() for entry in item.declarations)
        )
        records = tuple(
            item
            for item in records
            if any(term in field.casefold() for field in item.fields)
            or any(term in entry.label.casefold() for entry in item.declarations)
        )

    if args.kind == "value-set":
        records = ()
    elif args.kind == "record":
        value_sets = ()

    if args.json:
        print(
            json.dumps(
                {
                    "snapshot_id": snapshot_id,
                    "stale": drift,
                    "value_sets": [item.to_dict() for item in value_sets],
                    "records": [item.to_dict() for item in records],
                    "ambiguous_labels": list(ambiguous),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if drift:
        print(drift)
        print()
    for item in value_sets:
        members = ", ".join(item.members)
        print(f"value set: {members}")
        print(f"  {len(item.declarations):,} site(s) across {len(item.kinds):,} form(s)")
        for entry in item.declarations:
            print(f"  {entry.kind:18} {entry.path}:{entry.line} ({entry.label})")
        print()
    for record in records:
        names = " + ".join(shape.label for shape in record.declarations)
        print(f"record: {names} [{record.relation}, {len(record.fields):,} shared field(s)]")
        for shape in record.declarations:
            print(f"  {shape.kind:18} {shape.path}:{shape.line}")
        print()
    if not value_sets and not records:
        # An empty answer is a result. Saying so beats printing nothing and
        # letting a caller wonder whether the command ran.
        scope = f" matching {args.term!r}" if args.term else ""
        print(f"No contract declared in more than one form{scope}.")
    return 0


def _refusal_rows(symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every raise a symbol can reach, with the text it gives the caller.

    Kept out of the command body so the shape is testable without argparse.
    A bare re-raise carries no label worth printing and is dropped: it says
    the failure continues outward, which the caller already learns from the
    site that produced it.
    """

    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        metadata = symbol.get("metadata")
        if not isinstance(metadata, dict):
            continue
        flow = metadata.get("control_flow")
        if not isinstance(flow, list):
            continue
        refusals = [
            {
                "line": int(event.get("line", 0) or 0),
                "label": str(event.get("label", "")),
                "message": str(event["message"]) if event.get("message") else None,
            }
            for event in flow
            if isinstance(event, dict)
            and event.get("kind") == "raise"
            and str(event.get("label", "")) not in {"", "re-raise"}
        ]
        if not refusals:
            continue
        rows.append(
            {
                "symbol": str(symbol.get("qualified_name", "")),
                "path": str(symbol.get("path", "")),
                "start_line": int(symbol.get("start_line", 1) or 1),
                "refusals": sorted(refusals, key=lambda item: item["line"]),
            }
        )
    rows.sort(key=lambda item: (item["path"], item["start_line"]))
    return rows


def _refusals(args: argparse.Namespace) -> int:
    """Answer "what does this refuse with, and what does it say?"

    A status code tells a caller a request failed. The message tells them
    which failure it was, and it is the string they will search for when it
    appears in a log. Both are already in the ledger; reaching them meant
    reading a rendered specification or the source itself.
    """

    ledger, snapshot_id = _ledger_and_snapshot(args)
    drift = _snapshot_drift(_resolve_root(args.path), ledger, snapshot_id)
    rows = _refusal_rows(_every_symbol(ledger, snapshot_id))

    term = (args.term or "").casefold()
    if term:
        rows = [
            row
            for row in rows
            if term in row["symbol"].casefold()
            or term in row["path"].casefold()
            or any(
                term in item["label"].casefold()
                or (item["message"] or "").casefold().find(term) >= 0
                for item in row["refusals"]
            )
        ]
    if args.with_message:
        rows = [
            {**row, "refusals": [item for item in row["refusals"] if item["message"]]}
            for row in rows
        ]
        rows = [row for row in rows if row["refusals"]]

    rows = rows[: args.limit]
    if args.json:
        print(
            json.dumps(
                {"snapshot_id": snapshot_id, "stale": drift, "refusals": rows},
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if drift:
        print(drift)
        print()
    for row in rows:
        print(f"{row['symbol']}  ({row['path']}:{row['start_line']})")
        for item in row["refusals"]:
            quoted = f' "{item["message"]}"' if item["message"] else ""
            print(f"  {item['label']}{quoted}  :{item['line']}")
        print()
    if not rows:
        # "None found" and "none looked for" read identically, and the
        # difference is the whole question. The guard-and-exit trace is kept
        # only for route handlers and functions that actually branch, so a
        # plain function raising once has no trace to report and must not be
        # reported as refusing nothing.
        scope = f" matching {args.term!r}" if args.term else ""
        print(
            f"No refusal recorded{scope}. Refusals are read from the guard-and-exit "
            "trace, which is kept for route handlers and functions with at least two "
            "guards; a symbol without one is untraced rather than known to refuse "
            "nothing."
        )
    return 0


def _coverage_report(
    files: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    symbols: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
) -> dict[str, Any]:
    """What this analysis saw, and what it did not.

    Every absence in a specification is only as strong as the read it rests
    on. "This repository does not authenticate" and "the files that would
    have shown it were never opened" produce the same silence, and the
    difference is the whole question an auditor is asking. Assembled here so
    it can be asked directly instead of inferred from a document.
    """

    touched = {str(item["path"]) for item in symbols}
    touched.update(str(item["path"]) for item in evidence if item.get("path"))

    reasons: dict[str, int] = {}
    for item in exclusions:
        reason = str(item.get("reason", "unknown"))
        reasons[reason] = reasons.get(reason, 0) + max(1, int(item.get("contained_files") or 0))

    thin: list[dict[str, Any]] = []
    for record in coverage:
        eligible = int(record.get("eligible_files", 0) or 0)
        analyzed = int(record.get("analyzed_files", 0) or 0)
        if eligible > analyzed:
            thin.append(
                {
                    "analyzer": str(record.get("analyzer", "")),
                    "language": str(record.get("language", "")),
                    "eligible_files": eligible,
                    "analyzed_files": analyzed,
                    "failures": list(record.get("failures") or []),
                }
            )

    # A language an analyzer declared eligible and then failed to parse is
    # already reported as a parse shortfall, with the reason attached. Naming
    # it again as unread states one cause twice and reads as two, which is the
    # defect this same distinction had to be corrected for in the document.
    claimed = {record["language"] for record in thin}
    unread = [
        {"language": language, "files": count}
        for language, count in _languages_no_analyzer_read(
            tuple(files), tuple(symbols), tuple(evidence)
        )
        if language not in claimed
    ]

    return {
        "included_files": len(files),
        "excluded_files": sum(reasons.values()),
        "exclusion_reasons": dict(sorted(reasons.items(), key=lambda pair: (-pair[1], pair[0]))),
        "languages_no_analyzer_read": unread,
        "eligible_but_unparsed": thin,
    }


def _coverage(args: argparse.Namespace) -> int:
    """Answer "can I trust an absence here?" before trusting one."""

    ledger, snapshot_id = _ledger_and_snapshot(args)
    drift = _snapshot_drift(_resolve_root(args.path), ledger, snapshot_id)
    report = _coverage_report(
        list(ledger.list_files(snapshot_id)),
        list(ledger.list_exclusions(snapshot_id)),
        _every_symbol(ledger, snapshot_id),
        list(ledger.list_evidence(snapshot_id)),
        list(ledger.analysis_coverage(snapshot_id)),
    )
    if args.json:
        print(
            json.dumps(
                {"snapshot_id": snapshot_id, "stale": drift, **report},
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if drift:
        print(drift)
        print()
    print(f"included files: {report['included_files']:,}")
    print(f"excluded files: {report['excluded_files']:,}")
    for reason, count in list(report["exclusion_reasons"].items())[: args.limit]:
        print(f"  {count:>8,}  {reason}")
    unread = report["languages_no_analyzer_read"]
    if unread:
        named = ", ".join(f"{item['language']} ({item['files']:,})" for item in unread)
        print(f"\nno analyzer is equipped to read: {named}")
    for record in report["eligible_but_unparsed"]:
        missed = record["eligible_files"] - record["analyzed_files"]
        print(
            f"\n{record['analyzer']}: {missed:,} of {record['eligible_files']:,} "
            f"{record['language']} file(s) eligible but not parsed"
        )
        for failure in record["failures"][:3]:
            print(f"  {failure}")
    if not unread and not report["eligible_but_unparsed"]:
        print("\nEvery included file was read by an analyzer equipped for it.")
    return 0


def _claims(args: argparse.Namespace) -> int:
    ledger, snapshot_id = _ledger_and_snapshot(args)
    results = ledger.list_claims(
        snapshot_id,
        status=args.status,
        category=args.category,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for claim in results:
            print(
                f"[{claim['importance']}/{claim['status']}] {claim['category']}: {claim['claim']}"
            )
        print(f"\n{len(results):,} claims")
    return 0


def _audit(args: argparse.Namespace) -> int:
    ledger, snapshot_id = _ledger_and_snapshot(args)
    # Auditing a page and reporting the result as an audit is worse than
    # not auditing: the finding list looks complete either way.
    findings = audit_claims(
        tuple(every_claim(ledger, snapshot_id)),
        tuple(ledger.list_evidence(snapshot_id)),
        tuple(ledger.list_files(snapshot_id)),
    )
    if args.json:
        print(json.dumps([item.to_dict() for item in findings], indent=2, sort_keys=True))
    else:
        for finding in findings:
            print(f"[{finding.check}] {finding.category}: {finding.detail}")
        if findings:
            print(f"\n{len(findings):,} claim groups worth reading before publishing a number.")
        else:
            print("No claim group matches a known mistake shape.")
    # A finding is a place to look, not a defect, so this is opt-in: wiring it
    # into CI as a hard gate would make the honest response to an unusual
    # repository a red build.
    return 1 if (findings and args.strict) else 0


def _search(args: argparse.Namespace) -> int:
    """Answer a question from the ledger, in as few words as it takes.

    Claims say what this engine concluded. Declarations say what the source
    states outright -- a retry limit, a validation pattern, a vocabulary --
    and searching only the first answered 4 of 16 questions on a library whose
    subject is almost entirely declared values. Both are searched, and the
    declarations are printed with the file and line so an answer can be
    checked rather than trusted.
    """

    ledger, snapshot_id = _ledger_and_snapshot(args)
    results = ledger.search_claims(snapshot_id, args.query, limit=args.limit)
    declarations = ledger.search_declarations(snapshot_id, args.query, limit=args.limit)
    if args.json:
        print(
            json.dumps({"claims": results, "declarations": declarations}, indent=2, sort_keys=True)
        )
        return 0
    for claim in results:
        print(f"[{claim['status']}] {claim['claim']}")
    for symbol in declarations:
        for line in symbol["declares"].splitlines():
            print(f"[declared] {symbol['qualified_name']}: {line}  ({symbol['path']})")
    return 0


def _evidence(args: argparse.Namespace) -> int:
    root = _resolve_root(args.path)
    state_dir = _resolve_state_dir(root, args.state_dir)
    result = EvidenceLedger(state_dir / "evidence.sqlite3").get_evidence(args.evidence_id)
    if result is None:
        print(f"Evidence not found: {args.evidence_id}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _diff(args: argparse.Namespace) -> int:
    root = _resolve_root(args.path)
    state_dir = _resolve_state_dir(root, args.state_dir)
    ledger = EvidenceLedger(state_dir / "evidence.sqlite3")
    history = ledger.snapshots_for_root(root, limit=20)
    current = args.to_snapshot or (str(history[0]["snapshot_id"]) if history else None)
    previous = args.from_snapshot
    if previous is None:
        previous = next(
            (str(item["snapshot_id"]) for item in history if str(item["snapshot_id"]) != current),
            None,
        )
    if previous is None or current is None:
        raise ValueError("Two snapshots are required; analyze the repository after a change")
    difference = ledger.diff_snapshots(previous, current)
    difference["stale_claims"] = ledger.stale_claims(current)
    if args.json:
        print(json.dumps(difference, indent=2, sort_keys=True))
    else:
        print(f"From: {previous}")
        print(f"To:   {current}")
        print(f"Added: {len(difference['added']):,}")
        print(f"Removed: {len(difference['removed']):,}")
        print(f"Changed: {len(difference['changed']):,}")
        print(f"Stale claims: {len(difference['stale_claims']):,}")
    return 0


def _synthesize(args: argparse.Namespace) -> int:
    root = _resolve_root(args.path)
    state_dir = _resolve_state_dir(root, args.state_dir)
    ledger = EvidenceLedger(state_dir / "evidence.sqlite3")
    latest = ledger.latest_snapshot()
    if latest is None:
        raise ValueError("No analysis exists; run `open-skeleton analyze` first")
    snapshot_id = str(latest["snapshot_id"])
    context_pack = ledger.context_pack(
        snapshot_id,
        args.query,
        max_chars=args.max_chars,
        max_claims=args.max_claims,
    )
    request = ProviderRequest(
        task=args.task,
        snapshot_id=snapshot_id,
        context_pack=context_pack,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
    )
    adapter: ProviderAdapter
    if args.provider == "disabled":
        adapter = DisabledProvider()
    elif args.provider == "codex":
        adapter = CodexCliProvider()
    elif args.provider == "claude":
        adapter = ClaudeCliProvider()
    else:
        if not args.provider_command:
            raise ValueError("--provider local-command requires --command")
        adapter = LocalCommandProvider(args.provider_command)
    provider_workspace = state_dir / "provider-runs"
    provider_workspace.mkdir(parents=True, exist_ok=True)
    result = adapter.generate(request, workspace=provider_workspace)
    output_path = provider_workspace / f"{result.request_sha256[:16]}-{result.provider}.json"
    output_path.write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    response = {**result.to_dict(), "artifact": str(output_path)}
    if args.json:
        print(json.dumps(response, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(f"Provider: {result.provider}")
        print(f"Status: {result.status}")
        print(f"Request: {result.request_sha256}")
        print(f"Artifact: {output_path}")
        if result.error:
            print(f"Error: {result.error}")
    return 0 if result.status in {"complete", "disabled"} else 1


def _plan_synthesis(args: argparse.Namespace) -> int:
    root = _resolve_root(args.path)
    state_dir = _resolve_state_dir(root, args.state_dir)
    ledger = EvidenceLedger(state_dir / "evidence.sqlite3")
    profile = load_profile(args.profile)
    document = build_spec(ledger, profile, snapshot_id=args.snapshot)
    plan = build_synthesis_plan(
        document,
        ledger,
        max_chars=args.max_chars,
        max_claims=args.max_claims,
    )
    output = (args.output or state_dir / "synthesis-plan.json").expanduser().resolve()
    validate_external_output(output, root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    summary = {
        "snapshot_id": plan["snapshot_id"],
        "profile_id": plan["profile_id"],
        "job_count": plan["job_count"],
        "priority_counts": plan["priority_counts"],
        "verdict_counts": plan["verdict_counts"],
        "contacts_model": plan["contacts_model"],
        "artifact": str(output),
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(f"Snapshot: {plan['snapshot_id']}")
        print(f"Jobs: {plan['job_count']:,}")
        print("Model contacted: no")
        print(f"Plan: {output}")
    return 0


def _run_synthesis_plan(args: argparse.Namespace) -> int:
    root = _resolve_root(args.path)
    state_dir = _resolve_state_dir(root, args.state_dir)
    plan_path = (args.plan or state_dir / "synthesis-plan.json").expanduser().resolve()
    if args.provider == "codex":
        adapter: ProviderAdapter = CodexCliProvider()
    elif args.provider == "claude":
        adapter = ClaudeCliProvider()
    else:
        if not args.provider_command:
            raise ValueError("--provider local-command requires --command")
        adapter = LocalCommandProvider(args.provider_command)
    output_dir = (
        (args.output_dir or state_dir / "synthesis-runs" / adapter.name).expanduser().resolve()
    )
    summary = run_synthesis_plan(
        plan_path,
        source_root=root,
        output_dir=output_dir,
        adapter=adapter,
        execute=args.execute,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        concurrency=args.concurrency,
        max_jobs=args.max_jobs,
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(f"Provider: {summary['provider']}")
        print(f"Mode: {'execute' if summary['execute'] else 'dry-run'}")
        print(f"Jobs: {summary['job_count']:,}")
        print(f"Results: {summary['output_dir']}")
    failures = int(summary["status_counts"].get("error", 0))
    return 1 if args.execute and failures else 0


def _assemble_synthesis(args: argparse.Namespace) -> int:
    root = _resolve_root(args.path)
    state_dir = _resolve_state_dir(root, args.state_dir)
    plan_path = (args.plan or state_dir / "synthesis-plan.json").expanduser().resolve()
    output = (args.output or state_dir / "source-grounded-synthesis.md").expanduser().resolve()
    summary = assemble_synthesis(
        plan_path,
        results_dir=args.results_dir,
        source_root=root,
        output_path=output,
        max_jobs=args.max_jobs,
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(f"Jobs assembled: {summary['job_count']:,}")
        print("Model contacted: no")
        print(f"Document: {summary['artifact']}")
    return 0


def _benchmark(args: argparse.Namespace) -> int:
    result = run_benchmark(Path(args.path), args.gold, args.output_dir)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        current = result["open_skeleton"]
        print(f"Recall: {current['recall']:.1%}")
        print(f"Scoped precision: {current['precision']:.1%}")
        print(f"Evidence correctness: {current['evidence_correctness']:.1%}")
        print(f"Report: {args.output_dir / 'benchmark.md'}")
    return 0


def _spec(args: argparse.Namespace) -> int:
    root = _resolve_root(args.path)
    state_dir = _resolve_state_dir(root, args.state_dir)
    ledger = EvidenceLedger(state_dir / "evidence.sqlite3")
    profile = load_profile(args.profile)
    document = build_spec(ledger, profile, snapshot_id=args.snapshot)

    output_dir = (args.output_dir or state_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / "spec.md"
    json_path = output_dir / "spec.json"
    # The symbol inventory and name concordance scale with the repository
    # rather than with what is interesting in it, and they were 37% of a
    # six-megabyte file a consumer had to parse in full to read one section.
    index_path = output_dir / "spec.index.json"
    markdown = render_spec_markdown(document)
    markdown_path.write_text(markdown, encoding="utf-8", newline="\n")
    json_path.write_text(render_spec_json(document), encoding="utf-8", newline="\n")
    index_path.write_text(render_spec_index_json(document), encoding="utf-8", newline="\n")

    verdicts: dict[str, int] = {}
    for section in document.sections:
        verdicts[section.verdict] = verdicts.get(section.verdict, 0) + 1

    exercised = sum(1 for item in document.capabilities if item.verification == "exercised")
    summary: dict[str, object] = {
        "snapshot_id": document.snapshot_id,
        "profile": document.profile_id,
        "sections": len(document.sections),
        "verdicts": verdicts,
        "capabilities": len(document.capabilities),
        "capabilities_exercised": exercised,
        "total_claims": document.total_claims,
        "cited_claims": document.cited_claims,
        "stale_claim_count": document.stale_claim_count,
        "markdown": str(markdown_path),
        "json": str(json_path),
        "index": str(index_path),
    }

    report = None
    # Citation integrity says every receipt resolves. It cannot say the
    # document agrees with itself, and a specification that cites perfectly
    # can still announce a concern absent directly above its own findings.
    incoherences = check_coherence(document, markdown) + check_conservation(
        document,
        {
            "claims": ledger.count_rows(document.snapshot_id, "claims"),
            "symbols": ledger.count_rows(document.snapshot_id, "symbols"),
            "edges": ledger.count_rows(document.snapshot_id, "edges"),
        },
    )
    summary["incoherences"] = [
        {"check": item.check, "detail": item.detail} for item in incoherences
    ]
    if args.verify:
        report = verify_spec(document, ledger, root=root)
        summary["citation_integrity"] = report.integrity
        summary["citations"] = report.to_dict()

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(f"Snapshot: {document.snapshot_id}")
        print(f"Profile: {document.profile_id}")
        print(f"Sections: {len(document.sections):,}")
        for verdict in sorted(verdicts):
            print(f"  {verdict}: {verdicts[verdict]:,}")
        print(f"Claims rendered with receipts: {document.cited_claims:,}")
        if document.capabilities:
            print(
                f"Capabilities: {len(document.capabilities):,} "
                f"({exercised:,} reached by a test or harness)"
            )
        if report is not None:
            print(
                f"Citation integrity: {report.integrity:.1%} "
                f"({report.total:,} citations, {len(report.failures):,} failing)"
            )
        if incoherences:
            print(f"Self-consistency: {len(incoherences):,} statement(s) disagree with the data")
            for item in incoherences:
                print(f"  {item}")
        print(f"Spec: {markdown_path}")
    if incoherences:
        return 1
    if report is not None and report.failures:
        return 1
    return 0


def _serve(args: argparse.Namespace) -> int:
    root = _resolve_root(args.path)
    state_dir = _resolve_state_dir(root, args.state_dir)
    serve_dashboard(root, state_dir, host=args.host, port=args.port)
    return 0


def _use_utf8_output() -> None:
    """Write answers as UTF-8 whatever the console's own encoding is.

    Every file this tool writes is UTF-8 already; standard output was not.
    On a Windows console that meant an em dash left as the single cp1252 byte
    `\x97`, so a caller decoding the output as UTF-8 -- which is what a caller
    does -- got a decode error or a replacement character, and a quoted
    message stopped being the string it quoted.

    That matters more here than it looks. The point of these commands is to
    hand an exact message to something that will search for it, and a message
    that arrives mangled is worse than one that never arrived: it looks
    usable. JSON output was always correct, because JSON escapes non-ASCII;
    the text form is what needed fixing.

    A redirected stream in a test has no `reconfigure`, and a closed or
    detached one raises. Both are left alone.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            continue


def main(argv: Sequence[str] | None = None) -> int:
    _use_utf8_output()
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return _scan(args)
        if args.command == "analyze":
            return _analyze(args)
        if args.command == "status":
            return _status(args)
        if args.command == "coverage":
            return _coverage(args)
        if args.command == "refusals":
            return _refusals(args)
        if args.command == "contracts":
            return _contracts(args)
        if args.command == "claims":
            return _claims(args)
        if args.command == "audit":
            return _audit(args)
        if args.command == "search":
            return _search(args)
        if args.command == "evidence":
            return _evidence(args)
        if args.command == "diff":
            return _diff(args)
        if args.command == "synthesize":
            return _synthesize(args)
        if args.command == "plan-synthesis":
            return _plan_synthesis(args)
        if args.command == "run-synthesis-plan":
            return _run_synthesis_plan(args)
        if args.command == "assemble-synthesis":
            return _assemble_synthesis(args)
        if args.command == "benchmark":
            return _benchmark(args)
        if args.command == "spec":
            return _spec(args)
        if args.command == "serve":
            return _serve(args)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"open-skeleton: {exc}", file=sys.stderr)
        return 2
    parser.error(f"Unsupported command: {args.command}")
    return 2
