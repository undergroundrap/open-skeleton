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
from open_skeleton.state import resolve_state_dir


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


def _ledger_and_snapshot(args: argparse.Namespace) -> tuple[EvidenceLedger, str]:
    root = _resolve_root(args.path)
    state_dir = _resolve_state_dir(root, args.state_dir)
    ledger = EvidenceLedger(state_dir / "evidence.sqlite3")
    if args.snapshot:
        return ledger, args.snapshot
    latest = ledger.latest_snapshot()
    if latest is None:
        raise ValueError(f"No snapshot found in {state_dir}")
    return ledger, str(latest["snapshot_id"])


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
    ledger, snapshot_id = _ledger_and_snapshot(args)
    results = ledger.search_claims(snapshot_id, args.query, limit=args.limit)
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for claim in results:
            print(f"[{claim['status']}] {claim['claim']}")
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return _scan(args)
        if args.command == "analyze":
            return _analyze(args)
        if args.command == "status":
            return _status(args)
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
