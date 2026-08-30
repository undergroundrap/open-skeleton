# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Measure this engine against one registered external specification export.

The baseline registry pins the only two external documents used to evaluate
Open Skeleton. The script refuses an artifact whose bytes do not match
the selected record, a repository at the wrong revision, or a dirty fixture.
That keeps a result from silently comparing different documents or snapshots.

Usage:

    python benchmarks/comparison/run_comparison.py \\
        --repository <path to fixture> \\
        --baseline <path to the baseline tech_spec.md> \\
        --baseline-id <registered baseline id> \\
        --output-dir <where to write the report>

The baseline artifact is not redistributed with this repository. Supply your
own export to reproduce the comparison.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MERMAID_FENCE = "```mermaid"
FILE_REFERENCE = re.compile(r"`([\w./-]+\.(?:py|tsx|ts|js|jsx|css|md|json|toml|txt|rs))(:(\d+))?")
BASELINE_SCHEMA = "open-skeleton.comparison-baselines.v1"
COMPARISON_SCHEMA = "open-skeleton.comparison.v1"
DEFAULT_BASELINE_INVENTORY = Path(__file__).with_name("baselines.json")


def _load_baseline_record(path: Path, baseline_id: str) -> dict[str, Any]:
    """Load one uniquely named record from the public baseline inventory."""

    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != BASELINE_SCHEMA:
        raise ValueError(f"Unsupported baseline inventory schema in {path}")
    records = document.get("baselines")
    if not isinstance(records, list) or not records:
        raise ValueError("Baseline inventory must contain a non-empty baselines list")

    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise ValueError("Every baseline record must have a string id")
        record_id = record["id"]
        if record_id in by_id:
            raise ValueError(f"Duplicate baseline id: {record_id}")
        by_id[record_id] = record
    if baseline_id not in by_id:
        available = ", ".join(sorted(by_id))
        raise ValueError(f"Unknown baseline id {baseline_id!r}; available: {available}")

    record = by_id[baseline_id]
    for key in ("provider", "label", "artifact", "repository", "generation"):
        if key not in record:
            raise ValueError(f"Baseline {baseline_id!r} is missing {key!r}")
    return record


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_baseline_artifact(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Bind the supplied private export to its public hash-and-size receipt."""

    artifact = record["artifact"]
    expected_hash = artifact.get("sha256")
    expected_bytes = artifact.get("bytes")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
        raise ValueError(f"Baseline {record['id']!r} has an invalid artifact hash")
    if not isinstance(expected_bytes, int) or expected_bytes < 1:
        raise ValueError(f"Baseline {record['id']!r} has an invalid artifact size")

    observed_bytes = path.stat().st_size
    observed_hash = _sha256(path)
    if observed_bytes != expected_bytes or observed_hash.casefold() != expected_hash.casefold():
        raise ValueError(
            f"Baseline artifact does not match {record['id']!r}: "
            f"expected {expected_bytes} bytes / {expected_hash.casefold()}, "
            f"observed {observed_bytes} bytes / {observed_hash}"
        )
    return {
        "bytes": observed_bytes,
        "sha256": observed_hash,
        "verified": True,
    }


def _validate_repository_state(
    observed_commit: str, status_porcelain: str, record: dict[str, Any]
) -> dict[str, Any]:
    """Validate Git observations separately so adversarial cases need no Git process."""

    repository = record["repository"]
    expected_commit = repository.get("commit")
    if not isinstance(expected_commit, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", expected_commit
    ):
        raise ValueError(f"Baseline {record['id']!r} has an invalid repository commit")
    if observed_commit.casefold() != expected_commit.casefold():
        raise ValueError(
            f"Repository revision does not match {record['id']!r}: "
            f"expected {expected_commit.casefold()}, observed {observed_commit.casefold()}"
        )
    if status_porcelain.strip():
        raise ValueError(
            "Repository fixture has tracked or untracked changes; use a clean checkout so "
            "the commit identifies the bytes being compared"
        )
    return {
        "clean": True,
        "commit": observed_commit.casefold(),
        "revision_basis": repository["revision_basis"],
        "revision_certainty": repository["revision_certainty"],
    }


def _verify_repository(repository: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Require the selected baseline's clean, best-known repository revision."""

    git = shutil.which("git")
    if git is None:
        raise ValueError("git is required to verify the comparison fixture")

    def run(*arguments: str) -> str:
        completed = subprocess.run(  # noqa: S603
            [git, "-C", str(repository), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise ValueError(f"Could not verify repository fixture: {message}")
        return completed.stdout.strip()

    observed_commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain", "--untracked-files=all")
    return _validate_repository_state(observed_commit, status, record)


def _diagram_kinds(text: str) -> collections.Counter[str]:
    """Count Mermaid blocks by their declared diagram type."""

    kinds: collections.Counter[str] = collections.Counter()
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != MERMAID_FENCE:
            continue
        following = next(
            (
                lines[cursor].strip()
                for cursor in range(index + 1, index + 4)
                if lines[cursor].strip()
            ),
            "",
        )
        kinds[following.split()[0] if following else "unknown"] += 1
    return kinds


def _citations(text: str) -> tuple[int, int, int]:
    """Return total file references, those carrying a line, and distinct files."""

    matches = FILE_REFERENCE.findall(text)
    with_line = [item for item in matches if item[2]]
    return len(matches), len(with_line), len({item[0] for item in matches})


def _measure_open_skeleton(repository: Path, output_dir: Path) -> dict[str, Any]:
    """Run analyze and spec end to end, timing the whole path."""

    state = output_dir / "state"
    started = time.perf_counter()
    for command in (
        [
            sys.executable,
            "-m",
            "open_skeleton",
            "analyze",
            str(repository),
            "--state-dir",
            str(state),
            "--quiet",
        ],
        [
            sys.executable,
            "-m",
            "open_skeleton",
            "spec",
            str(repository),
            "--state-dir",
            str(state),
            "--verify",
            "--json",
        ],
    ):
        completed = subprocess.run(  # noqa: S603
            command, capture_output=True, text=True, check=False
        )
        if completed.returncode != 0:
            raise SystemExit(f"command failed: {' '.join(command)}\n{completed.stderr}")
        last = completed.stdout
    elapsed = time.perf_counter() - started

    summary = json.loads(last)
    document_path = state / "spec.md"
    document = document_path.read_text(encoding="utf-8")
    total, with_line, distinct = _citations(document)
    return {
        "seconds": round(elapsed, 3),
        "artifact_bytes": document_path.stat().st_size,
        "artifact_sha256": _sha256(document_path),
        "words": len(document.split()),
        "sections": summary["sections"],
        "diagrams": dict(_diagram_kinds(document)),
        "diagram_total": sum(_diagram_kinds(document).values()),
        "file_references": total,
        "references_with_line": with_line,
        "distinct_files_cited": distinct,
        "machine_verified_citations": summary["citations"]["total_citations"],
        "citation_integrity": summary["citation_integrity"],
        "capabilities": summary["capabilities"],
        "capabilities_exercised": summary["capabilities_exercised"],
    }


def _measure_baseline(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    document = path.read_text(encoding="utf-8")
    total, with_line, distinct = _citations(document)
    kinds = _diagram_kinds(document)
    total_time_ms = record["generation"].get("total_time_ms")
    seconds = None if total_time_ms is None else total_time_ms / 1000
    return {
        "seconds": seconds,
        "artifact_bytes": path.stat().st_size,
        "artifact_sha256": _sha256(path),
        "words": len(document.split()),
        "sections": len(re.findall(r"^#{1,2} ", document, flags=re.MULTILINE)),
        "diagrams": dict(kinds),
        "diagram_total": sum(kinds.values()),
        "file_references": total,
        "references_with_line": with_line,
        "distinct_files_cited": distinct,
        "machine_verified_citations": 0,
        "citation_integrity": None,
        "capabilities": None,
        "capabilities_exercised": None,
    }


def _render(
    ours: dict[str, Any],
    theirs: dict[str, Any],
    record: dict[str, Any],
    repository_receipt: dict[str, Any],
) -> str:
    def row(name: str, key: str, fmt: str = "{:,}") -> str:
        left = ours.get(key)
        right = theirs.get(key)
        render = lambda value: "not reported" if value is None else fmt.format(value)  # noqa: E731
        return f"| {name} | {render(left)} | {render(right)} |\n"

    baseline_name = f"{record['provider']} baseline"
    lines = [
        "# Specification comparison\n\n",
        (
            "The supplied baseline bytes and the fixture revision were verified "
            f"against registered record `{record['id']}` before measurement. "
            f"The baseline is {record['label']}.\n\n"
        ),
        f"| Measure | Open Skeleton | {baseline_name} |\n|---|---:|---:|\n",
        row("Generation time (seconds)", "seconds", "{:,.1f}"),
        row("Diagrams", "diagram_total"),
        row("Words", "words"),
        row("File references", "file_references"),
        row("References carrying a line number", "references_with_line"),
        row("Citations verified against source hashes", "machine_verified_citations"),
    ]
    integrity = ours.get("citation_integrity")
    if integrity is not None:
        lines.append(f"| Citation integrity | {integrity:.1%} | not reported |\n")
    if ours.get("seconds") and theirs.get("seconds"):
        speedup = theirs["seconds"] / ours["seconds"]
        time_fraction = ours["seconds"] / theirs["seconds"]
        lines.append(
            "\nOpen Skeleton used "
            f"{time_fraction:.4%} of the baseline's recorded wall time "
            f"(baseline/candidate elapsed-time ratio {speedup:,.1f}x). The baseline timing is an "
            "author-recorded historical observation, not a same-machine rerun.\n"
        )
    lines.append(
        f"\n## Provenance receipts\n\n"
        f"- Baseline provider: `{record['provider']}`\n"
        f"- Baseline record: `{record['id']}`\n"
        f"- Baseline SHA-256: `{theirs['artifact_sha256']}`\n"
        f"- Candidate SHA-256: `{ours['artifact_sha256']}`\n"
        f"- Repository commit: `{repository_receipt['commit']}`\n"
        f"- Revision certainty: `{repository_receipt['revision_certainty']}` - "
        f"{repository_receipt['revision_basis']}\n\n"
        "The private baseline artifact is not redistributed. Its Markdown export "
        "is the canonical measured document; an accompanying PDF is another "
        "rendering of the same export, not another baseline run.\n"
    )
    lines.append(
        f"\n## Diagrams by type\n\n| Type | Open Skeleton | {baseline_name} |\n|---|---:|---:|\n"
    )
    for kind in sorted(set(ours["diagrams"]) | set(theirs["diagrams"])):
        lines.append(
            f"| `{kind}` | {ours['diagrams'].get(kind, 0):,} | {theirs['diagrams'].get(kind, 0):,} |\n"
        )
    lines.append(
        "\n## What these numbers do and do not show\n\n"
        "Every figure above is counted from the two hash-identified documents "
        "on disk by `benchmarks/comparison/run_comparison.py`. Re-run it to "
        "reproduce them.\n\n"
        "The comparison measures the shape of two artifacts describing one "
        "repository. It does not measure correctness of the baseline's prose, and "
        "it does not claim the two documents attempt the same scope: the baseline "
        "carries a requirements catalog and interface analysis this engine does "
        "not produce. Word count is reported because it is asked about, not "
        "because more words are better.\n\n"
        "The citation rows are the ones that matter. A reference carrying a line "
        "number can be checked by a reader; one that names only a file cannot. A "
        "citation pinned to a content hash can be checked by a machine, which is "
        "what `open-skeleton spec --verify` does on every run.\n"
    )
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--baseline-id", required=True)
    parser.add_argument(
        "--baseline-inventory",
        type=Path,
        default=DEFAULT_BASELINE_INVENTORY,
        help="Hash-pinned baseline registry (defaults to the repository inventory).",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    inventory_path = args.baseline_inventory.resolve(strict=True)
    record = _load_baseline_record(inventory_path, args.baseline_id)
    baseline_path = args.baseline.resolve(strict=True)
    repository_path = args.repository.resolve(strict=True)
    baseline_receipt = _verify_baseline_artifact(baseline_path, record)
    repository_receipt = _verify_repository(repository_path, record)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ours = _measure_open_skeleton(repository_path, args.output_dir)
    theirs = _measure_baseline(baseline_path, record)

    report = _render(ours, theirs, record, repository_receipt)
    (args.output_dir / "comparison.md").write_text(report, encoding="utf-8", newline="\n")
    (args.output_dir / "comparison.json").write_text(
        json.dumps(
            {
                "schema_version": COMPARISON_SCHEMA,
                "repository_verification": repository_receipt,
                "open_skeleton": ours,
                "baseline": {
                    "artifact_verification": baseline_receipt,
                    "measurements": theirs,
                    "record": record,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
