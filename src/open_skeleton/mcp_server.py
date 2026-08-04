# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.exports import (
    export_analysis_jsonl,
    export_analysis_markdown,
    export_jsonl,
    export_markdown,
)
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository
from open_skeleton.state import resolve_state_dir


class OpenSkeletonService:
    """Repository-bound operations shared by MCP and contract tests."""

    def __init__(
        self,
        root: Path,
        state_dir: Path | None = None,
        *,
        hum_index: Sequence[Path] | Path | None = None,
    ) -> None:
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError(f"Repository root is not a directory: {self.root}")
        self.state_dir = resolve_state_dir(self.root, state_dir)
        if hum_index is None:
            supplied: tuple[Path, ...] = ()
        elif isinstance(hum_index, Path):
            supplied = (hum_index,)
        else:
            supplied = tuple(hum_index)
        self.hum_index = tuple(item.expanduser().resolve() for item in supplied)
        self.ledger = EvidenceLedger(self.state_dir / "evidence.sqlite3")

    def _latest_snapshot_id(self) -> str:
        latest = self.ledger.latest_snapshot()
        if latest is None:
            raise ValueError("No analysis exists. Call refresh_analysis first.")
        if Path(str(latest["root_path"])).resolve() != self.root:
            raise ValueError("The latest ledger snapshot belongs to a different repository root")
        return str(latest["snapshot_id"])

    def project_status(self) -> dict[str, Any]:
        """Return the latest content-pinned project and analysis status."""

        latest = self.ledger.latest_snapshot()
        if latest is None:
            return {
                "root": str(self.root),
                "state_dir": str(self.state_dir),
                "status": "not_analyzed",
            }
        latest["analysis"] = self.ledger.latest_analysis(str(latest["snapshot_id"]))
        latest["stale_claim_count"] = len(self.ledger.stale_claims(str(latest["snapshot_id"])))
        return latest

    def list_claims(
        self,
        status: Literal["verified", "inferred", "conflict", "unknown", "stale"] | None = None,
        category: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List atomic claims from the latest snapshot with evidence identifiers."""

        return self.ledger.list_claims(
            self._latest_snapshot_id(), status=status, category=category, limit=limit
        )

    def analysis_coverage(self) -> list[dict[str, Any]]:
        """Return per-adapter analysis coverage for the latest snapshot."""

        return self.ledger.analysis_coverage(self._latest_snapshot_id())

    def search_claims(self, query: str, limit: int = 30) -> list[dict[str, Any]]:
        """Search claims in the latest snapshot."""

        return self.ledger.search_claims(self._latest_snapshot_id(), query, limit=limit)

    def get_evidence(self, evidence_id: str, max_lines: int = 80) -> dict[str, Any]:
        """Return one receipt and a hash-verified, bounded source excerpt when still current."""

        receipt = self.ledger.get_evidence_excerpt(evidence_id, max_lines=max_lines)
        if receipt is None:
            raise ValueError(f"Evidence not found: {evidence_id}")
        return receipt

    def list_symbols(
        self,
        query: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List or filter symbols from the latest semantic inventory."""

        return self.ledger.list_symbols(
            self._latest_snapshot_id(), query=query, kind=kind, limit=limit
        )

    def get_symbol_neighbors(self, symbol_id: str, limit: int = 200) -> dict[str, Any]:
        """Return inbound and outbound relationships for one symbol."""

        return self.ledger.symbol_neighbors(self._latest_snapshot_id(), symbol_id, limit=limit)

    def build_context_pack(
        self, query: str, max_chars: int = 20_000, max_claims: int = 12
    ) -> dict[str, Any]:
        """Build a bounded evidence pack for an agent rather than dumping the repository."""

        return self.ledger.context_pack(
            self._latest_snapshot_id(),
            query,
            max_chars=max_chars,
            max_claims=max_claims,
        )

    def latest_diff(self) -> dict[str, Any]:
        """Compare the two newest distinct snapshots for this bound repository."""

        history = self.ledger.snapshots_for_root(self.root, limit=20)
        if len(history) < 2:
            raise ValueError("Two distinct snapshots are required")
        current = str(history[0]["snapshot_id"])
        previous = next(
            (
                str(item["snapshot_id"])
                for item in history[1:]
                if str(item["snapshot_id"]) != current
            ),
            None,
        )
        if previous is None:
            raise ValueError("Two distinct snapshots are required")
        result = self.ledger.diff_snapshots(previous, current)
        result["stale_claims"] = self.ledger.stale_claims(current)
        return result

    def refresh_analysis(self) -> dict[str, Any]:
        """Re-scan the bound root and write only to the configured local state directory."""

        previous_history = self.ledger.snapshots_for_root(self.root, limit=1)
        previous = str(previous_history[0]["snapshot_id"]) if previous_history else None
        snapshot = scan_repository(self.root)
        result = analyze_snapshot(snapshot, hum_index=self.hum_index)
        self.ledger.save_snapshot(snapshot)
        run_id = self.ledger.save_analysis(result)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        export_jsonl(snapshot, self.state_dir / "inventory.jsonl")
        export_markdown(snapshot, self.state_dir / "inventory.md")
        export_analysis_jsonl(result, self.state_dir / "analysis.jsonl")
        export_analysis_markdown(result, self.state_dir / "analysis.md")
        stale_count = 0
        difference: dict[str, Any] | None = None
        if previous and previous != snapshot.snapshot_id:
            difference = self.ledger.diff_snapshots(previous, snapshot.snapshot_id)
            stale_count = len(self.ledger.project_stale_claims(previous, snapshot.snapshot_id))
        return {
            **result.summary(),
            "run_id": run_id,
            "root": str(self.root),
            "state_dir": str(self.state_dir),
            "ledger_write": True,
            "target_repository_write": False,
            "diff": difference,
            "stale_claim_count": stale_count,
        }


def create_mcp_server(service: OpenSkeletonService) -> Any:
    """Create an official-SDK MCP server; the SDK is an optional dependency."""

    try:
        # Imported at call time because the SDK is an optional extra; a
        # top-level import would break the CLI when it is not installed.
        from mcp.server import MCPServer  # noqa: PLC0415
        from mcp.types import ToolAnnotations  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised in minimal installs.
        raise RuntimeError(
            "MCP support is not installed. Install Open Skeleton with the `mcp` extra."
        ) from exc

    server = MCPServer("Open Skeleton")
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    ledger_write = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )

    server.tool(title="Project status", annotations=read_only)(service.project_status)
    server.tool(title="List evidence-backed claims", annotations=read_only)(service.list_claims)
    server.tool(title="Get analysis coverage", annotations=read_only)(service.analysis_coverage)
    server.tool(title="Search claims", annotations=read_only)(service.search_claims)
    server.tool(title="Get verified evidence excerpt", annotations=read_only)(service.get_evidence)
    server.tool(title="List semantic symbols", annotations=read_only)(service.list_symbols)
    server.tool(title="Get symbol relationships", annotations=read_only)(
        service.get_symbol_neighbors
    )
    server.tool(title="Build bounded context pack", annotations=read_only)(
        service.build_context_pack
    )
    server.tool(title="Get latest snapshot diff", annotations=read_only)(service.latest_diff)
    server.tool(title="Refresh local analysis ledger", annotations=ledger_write)(
        service.refresh_analysis
    )
    return server


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="open-skeleton-mcp",
        description="Run a repository-bound Open Skeleton MCP server over stdio.",
    )
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--hum-index", type=Path)
    args = parser.parse_args(argv)
    service = OpenSkeletonService(
        Path(args.root), state_dir=args.state_dir, hum_index=args.hum_index
    )
    create_mcp_server(service).run()


if __name__ == "__main__":
    main()
