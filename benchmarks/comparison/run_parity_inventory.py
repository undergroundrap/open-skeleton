# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Freeze a loss-accounted, full-document semantic parity corpus.

The reasoning inventory intentionally selected high-salience causal prose. That
is useful for triage and insufficient for a one-to-one claim. This inventory
assigns every nonblank line in both documents to a stable content block:
headings, prose, list items, table rows, code examples, or diagrams. Review
agents may later classify presentation-only blocks and atomize material blocks,
but nothing disappears before that decision is made.

Private baseline text is copied into the resulting corpus. The output must be
outside the analyzed repository, this tool repository, and every Git worktree.
No model is invoked by this command.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from run_comparison import (
    DEFAULT_BASELINE_INVENTORY,
    _load_baseline_record,
    _sha256,
    _verify_baseline_artifact,
    _verify_repository,
)

SCHEMA = "open-skeleton.parity-corpus.v1"
HEADING = re.compile(r"^[ ]{0,3}(#{1,6})\s+(.+?)\s*$")
FENCE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")
TABLE_SEPARATOR = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")
LIST_ITEM = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
PRESENTATION = re.compile(r"^\s*(?:-{3,}|_{3,}|\*{3,})\s*$")
TOOL_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class ContentBlock:
    kind: str
    heading: str
    start_line: int
    end_line: int
    text: str

    def to_dict(self, prefix: str) -> dict[str, Any]:
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        identity = hashlib.sha256(
            f"{prefix}\n{self.kind}\n{self.heading}\n{self.start_line}\n{digest}".encode()
        ).hexdigest()[:20]
        return {
            "id": f"{prefix}-block-{identity}",
            "kind": self.kind,
            "heading": self.heading,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "text": self.text,
            "text_sha256": digest,
        }


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_private_output(output_dir: Path, *protected_roots: Path) -> Path:
    """Reject private-derived output anywhere Git could redistribute it."""

    resolved = output_dir.expanduser().resolve()
    for root in protected_roots:
        if _within(resolved, root.expanduser().resolve()):
            raise ValueError(f"Private-derived parity output cannot be written inside {root}")
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            raise ValueError(
                f"Private-derived parity output cannot be written inside Git worktree {parent}"
            )
    return resolved


def _content_blocks(document: str, *, prefix: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Segment Markdown while proving that every nonblank line was retained."""

    lines = document.splitlines()
    blocks: list[ContentBlock] = []
    accounted: set[int] = set()
    heading = ""
    paragraph: list[str] = []
    paragraph_lines: list[int] = []
    fence_lines: list[str] = []
    fence_line_numbers: list[int] = []
    active_fence: tuple[str, int, str] | None = None

    def emit(kind: str, values: list[str], line_numbers: list[int]) -> None:
        if not values or not line_numbers:
            return
        text = "\n".join(values).strip()
        if not text:
            return
        blocks.append(
            ContentBlock(
                kind=kind,
                heading=heading,
                start_line=line_numbers[0],
                end_line=line_numbers[-1],
                text=text,
            )
        )
        accounted.update(line_numbers)

    def flush_paragraph() -> None:
        nonlocal paragraph, paragraph_lines
        emit("prose", paragraph, paragraph_lines)
        paragraph = []
        paragraph_lines = []

    for line_number, line in enumerate(lines, start=1):
        if active_fence is not None:
            fence_lines.append(line)
            if line.strip():
                fence_line_numbers.append(line_number)
            match = FENCE.match(line)
            if match is not None:
                marker = match.group(1)
                if (
                    marker[0] == active_fence[0]
                    and len(marker) >= active_fence[1]
                    and not match.group(2).strip()
                ):
                    emit(active_fence[2], fence_lines, fence_line_numbers)
                    fence_lines = []
                    fence_line_numbers = []
                    active_fence = None
            continue

        fence = FENCE.match(line)
        if fence is not None:
            flush_paragraph()
            marker = fence.group(1)
            info = fence.group(2).strip().casefold()
            active_fence = (marker[0], len(marker), "diagram" if info == "mermaid" else "code")
            fence_lines = [line]
            fence_line_numbers = [line_number] if line.strip() else []
            continue

        heading_match = HEADING.match(line)
        if heading_match is not None:
            flush_paragraph()
            heading = heading_match.group(2).strip()
            emit("heading", [line], [line_number])
            continue

        if not line.strip():
            flush_paragraph()
            continue

        stripped = line.lstrip()
        if stripped.startswith("|"):
            flush_paragraph()
            kind = "presentation_only" if TABLE_SEPARATOR.match(line) else "table_row"
            emit(kind, [line], [line_number])
            continue
        if LIST_ITEM.match(line):
            flush_paragraph()
            emit("list_item", [line], [line_number])
            continue
        if PRESENTATION.match(line):
            flush_paragraph()
            emit("presentation_only", [line], [line_number])
            continue
        paragraph.append(line)
        paragraph_lines.append(line_number)

    flush_paragraph()
    if active_fence is not None:
        emit(active_fence[2], fence_lines, fence_line_numbers)

    nonblank = {index for index, line in enumerate(lines, start=1) if line.strip()}
    missing = sorted(nonblank - accounted)
    duplicated_line_count = sum(block.end_line - block.start_line + 1 for block in blocks) - len(
        accounted
    )
    if missing:
        raise ValueError(
            f"Parity corpus failed line accounting for {prefix}: "
            + ", ".join(str(item) for item in missing[:20])
        )
    if duplicated_line_count:
        # Blocks may span blank lines only when a future parser changes. No
        # content line may belong to two semantic decisions.
        seen: set[int] = set()
        overlap: set[int] = set()
        for block in blocks:
            for line_number in range(block.start_line, block.end_line + 1):
                if not lines[line_number - 1].strip():
                    continue
                if line_number in seen:
                    overlap.add(line_number)
                seen.add(line_number)
        if overlap:
            raise ValueError(
                f"Parity corpus assigned lines more than once for {prefix}: "
                + ", ".join(str(item) for item in sorted(overlap)[:20])
            )

    return [item.to_dict(prefix) for item in blocks], {
        "physical_lines": len(lines),
        "nonblank_lines": len(nonblank),
        "accounted_nonblank_lines": len(accounted),
        "unaccounted_lines": missing,
        "block_count": len(blocks),
        "block_kinds": dict(sorted(collections.Counter(item.kind for item in blocks).items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--baseline-id", required=True)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--baseline-inventory", type=Path, default=DEFAULT_BASELINE_INVENTORY)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    record = _load_baseline_record(args.baseline_inventory, args.baseline_id)
    baseline_receipt = _verify_baseline_artifact(args.baseline, record)
    repository_receipt = _verify_repository(args.repo, record)
    output_dir = _require_private_output(args.output_dir, args.repo, TOOL_ROOT)
    baseline_blocks, baseline_accounting = _content_blocks(
        args.baseline.read_text(encoding="utf-8"), prefix="baseline"
    )
    candidate_blocks, candidate_accounting = _content_blocks(
        args.candidate.read_text(encoding="utf-8"), prefix="candidate"
    )

    report = {
        "schema": SCHEMA,
        "sensitivity": "private-source-derived",
        "scope": "full-document",
        "contacts_model": False,
        "baseline": {
            "id": record["id"],
            "path": str(args.baseline.resolve()),
            **baseline_receipt,
        },
        "candidate": {
            "path": str(args.candidate.resolve()),
            "bytes": args.candidate.stat().st_size,
            "sha256": _sha256(args.candidate),
        },
        "repository": {"path": str(args.repo.resolve()), **repository_receipt},
        "context": (
            {
                "path": str(args.context.resolve()),
                "bytes": args.context.stat().st_size,
                "sha256": _sha256(args.context),
            }
            if args.context
            else None
        ),
        "accounting": {
            "baseline": baseline_accounting,
            "candidate": candidate_accounting,
        },
        "baseline_blocks": baseline_blocks,
        "candidate_blocks": candidate_blocks,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "parity-corpus.json"
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    summary = {
        "schema": report["schema"],
        "contacts_model": False,
        "baseline_blocks": len(baseline_blocks),
        "candidate_blocks": len(candidate_blocks),
        "baseline_nonblank_lines": baseline_accounting["nonblank_lines"],
        "baseline_accounted_lines": baseline_accounting["accounted_nonblank_lines"],
        "artifact": str(output),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
