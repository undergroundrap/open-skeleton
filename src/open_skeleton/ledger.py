# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from open_skeleton.ids import stable_id
from open_skeleton.models import AnalysisResult, Snapshot, utc_now

SCHEMA_VERSION = "4"


class EvidenceLedger:
    """SQLite-backed store for immutable snapshot facts and analysis events."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # Columns added after a schema version shipped. `CREATE TABLE IF NOT EXISTS`
    # leaves an existing table untouched, so a ledger written by an earlier
    # version keeps its old shape and every read of a new column fails. Each
    # entry must be nullable or carry a default; this path never rewrites or
    # drops stored rows.
    _ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
        # Nullable on purpose: a row written before this column existed has an
        # unknown yield, which is not the same as a yield of zero. Backfilling a
        # default would make a migrated ledger state a falsehood about itself.
        ("analysis_coverage", "claimed_files", "INTEGER"),
        # Zero is the right default here, unlike the yield above: a ledger
        # written before this column existed recorded one row per excluded
        # entry, and zero is what that row already meant.
        ("exclusions", "contained_files", "INTEGER NOT NULL DEFAULT 0"),
    )

    def _apply_additive_migrations(self, connection: sqlite3.Connection) -> None:
        for table, column, definition in self._ADDITIVE_COLUMNS:
            existing = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not existing or column in existing:
                continue
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._session() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
                    file_count INTEGER NOT NULL CHECK (file_count >= 0),
                    excluded_count INTEGER NOT NULL CHECK (excluded_count >= 0),
                    total_bytes INTEGER NOT NULL CHECK (total_bytes >= 0),
                    total_lines INTEGER NOT NULL CHECK (total_lines >= 0)
                );

                CREATE TABLE IF NOT EXISTS files (
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                    line_count INTEGER NOT NULL CHECK (line_count >= 0),
                    language TEXT NOT NULL,
                    role TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, path)
                );

                CREATE INDEX IF NOT EXISTS files_language_idx
                    ON files(snapshot_id, language);
                CREATE INDEX IF NOT EXISTS files_role_idx
                    ON files(snapshot_id, role);

                CREATE TABLE IF NOT EXISTS exclusions (
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    contained_files INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (snapshot_id, path, reason)
                );

                CREATE TABLE IF NOT EXISTS events (
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
                    event_order INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    path TEXT,
                    processed_files INTEGER,
                    PRIMARY KEY (snapshot_id, event_order)
                );

                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    start_line INTEGER,
                    end_line INTEGER,
                    symbol TEXT,
                    evidence_kind TEXT NOT NULL,
                    excerpt_sha256 TEXT,
                    analyzer TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS evidence_snapshot_path_idx
                    ON evidence(snapshot_id, path, start_line);

                CREATE TABLE IF NOT EXISTS symbols (
                    symbol_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    qualified_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    language TEXT NOT NULL,
                    analyzer TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS symbols_snapshot_name_idx
                    ON symbols(snapshot_id, qualified_name);
                CREATE INDEX IF NOT EXISTS symbols_snapshot_path_idx
                    ON symbols(snapshot_id, path, start_line);

                CREATE TABLE IF NOT EXISTS edges (
                    edge_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
                    source_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE CASCADE,
                    source_path TEXT NOT NULL,
                    relationship TEXT NOT NULL,
                    target_ref TEXT NOT NULL,
                    target_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
                    evidence_id TEXT REFERENCES evidence(evidence_id) ON DELETE SET NULL,
                    analyzer TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS edges_snapshot_relationship_idx
                    ON edges(snapshot_id, relationship);
                CREATE INDEX IF NOT EXISTS edges_target_ref_idx
                    ON edges(snapshot_id, target_ref);

                CREATE TABLE IF NOT EXISTS claims (
                    claim_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
                    claim TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('verified', 'inferred', 'conflict', 'unknown', 'stale')
                    ),
                    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
                    importance TEXT NOT NULL,
                    produced_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    verified_at TEXT
                );

                CREATE TABLE IF NOT EXISTS claim_evidence (
                    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
                    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
                    relationship TEXT NOT NULL CHECK (
                        relationship IN ('supports', 'contradicts')
                    ),
                    PRIMARY KEY (claim_id, evidence_id, relationship)
                );

                CREATE TABLE IF NOT EXISTS conflicts (
                    conflict_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    resolution TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS invalidation_keys (
                    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
                    invalidation_key TEXT NOT NULL,
                    PRIMARY KEY (claim_id, invalidation_key)
                );

                CREATE TABLE IF NOT EXISTS claim_alternatives (
                    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
                    alternative TEXT NOT NULL,
                    PRIMARY KEY (claim_id, alternative)
                );

                CREATE TABLE IF NOT EXISTS claim_validity (
                    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
                    evaluated_against_snapshot_id TEXT NOT NULL
                        REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK (status IN ('current', 'stale')),
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (claim_id, evaluated_against_snapshot_id)
                );

                CREATE INDEX IF NOT EXISTS claim_validity_target_idx
                    ON claim_validity(evaluated_against_snapshot_id, status);

                CREATE TABLE IF NOT EXISTS analysis_runs (
                    run_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
                    analyzer_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
                    symbol_count INTEGER NOT NULL CHECK (symbol_count >= 0),
                    edge_count INTEGER NOT NULL CHECK (edge_count >= 0),
                    evidence_count INTEGER NOT NULL CHECK (evidence_count >= 0),
                    claim_count INTEGER NOT NULL CHECK (claim_count >= 0)
                );

                CREATE TABLE IF NOT EXISTS analysis_coverage (
                    run_id TEXT NOT NULL REFERENCES analysis_runs(run_id) ON DELETE CASCADE,
                    analyzer TEXT NOT NULL,
                    language TEXT NOT NULL,
                    eligible_files INTEGER NOT NULL CHECK (eligible_files >= 0),
                    analyzed_files INTEGER NOT NULL CHECK (analyzed_files >= 0),
                    failed_files INTEGER NOT NULL CHECK (failed_files >= 0),
                    unsupported_files INTEGER NOT NULL CHECK (unsupported_files >= 0),
                    claimed_files INTEGER NOT NULL DEFAULT 0 CHECK (claimed_files >= 0),
                    failures_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, analyzer, language)
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
                    artifact_type TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._apply_additive_migrations(connection)
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            try:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS file_search USING fts5(
                        snapshot_id UNINDEXED,
                        path,
                        language,
                        role
                    )
                    """
                )
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES ('fts5', 'enabled')"
                )
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS claim_search USING fts5(
                        snapshot_id UNINDEXED,
                        claim_id UNINDEXED,
                        claim,
                        category,
                        status
                    )
                    """
                )
            except sqlite3.OperationalError:
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES ('fts5', 'unavailable')"
                )

    def save_snapshot(self, snapshot: Snapshot) -> None:
        self.initialize()
        observed_at = utc_now()
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO snapshots(
                    snapshot_id, root_path, policy_version, first_seen_at, last_seen_at,
                    duration_ms, file_count, excluded_count, total_bytes, total_lines
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    root_path = excluded.root_path,
                    last_seen_at = excluded.last_seen_at,
                    duration_ms = excluded.duration_ms,
                    file_count = excluded.file_count,
                    excluded_count = excluded.excluded_count,
                    total_bytes = excluded.total_bytes,
                    total_lines = excluded.total_lines
                """,
                (
                    snapshot.snapshot_id,
                    str(snapshot.root),
                    snapshot.policy_version,
                    snapshot.created_at,
                    observed_at,
                    snapshot.duration_ms,
                    len(snapshot.files),
                    len(snapshot.exclusions),
                    snapshot.total_bytes,
                    snapshot.total_lines,
                ),
            )

            connection.execute("DELETE FROM files WHERE snapshot_id = ?", (snapshot.snapshot_id,))
            connection.executemany(
                """
                INSERT INTO files(
                    snapshot_id, path, sha256, size_bytes, line_count, language, role
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        snapshot.snapshot_id,
                        item.path,
                        item.sha256,
                        item.size_bytes,
                        item.line_count,
                        item.language,
                        item.role,
                    )
                    for item in snapshot.files
                ),
            )

            connection.execute(
                "DELETE FROM exclusions WHERE snapshot_id = ?", (snapshot.snapshot_id,)
            )
            connection.executemany(
                "INSERT INTO exclusions(snapshot_id, path, reason, contained_files) "
                "VALUES (?, ?, ?, ?)",
                (
                    (snapshot.snapshot_id, item.path, item.reason, item.contained_files)
                    for item in snapshot.exclusions
                ),
            )

            connection.execute("DELETE FROM events WHERE snapshot_id = ?", (snapshot.snapshot_id,))
            connection.executemany(
                """
                INSERT INTO events(
                    snapshot_id, event_order, stage, message, created_at, path, processed_files
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        snapshot.snapshot_id,
                        index,
                        event.stage,
                        event.message,
                        event.created_at,
                        event.path,
                        event.processed_files,
                    )
                    for index, event in enumerate(snapshot.events)
                ),
            )

            fts_status = connection.execute(
                "SELECT value FROM metadata WHERE key = 'fts5'"
            ).fetchone()
            if fts_status is not None and fts_status["value"] == "enabled":
                connection.execute(
                    "DELETE FROM file_search WHERE snapshot_id = ?", (snapshot.snapshot_id,)
                )
                connection.executemany(
                    "INSERT INTO file_search(snapshot_id, path, language, role) VALUES (?, ?, ?, ?)",
                    (
                        (snapshot.snapshot_id, item.path, item.language, item.role)
                        for item in snapshot.files
                    ),
                )

    def latest_snapshot(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        self.initialize()
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT snapshot_id, root_path, policy_version, first_seen_at, last_seen_at,
                       duration_ms, file_count, excluded_count, total_bytes, total_lines
                FROM snapshots
                ORDER BY last_seen_at DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row is not None else None

    def snapshots_for_root(self, root: Path, *, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1_000:
            raise ValueError("Snapshot history limit must be between 1 and 1000")
        if not self.path.exists():
            return []
        self.initialize()
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_id, root_path, policy_version, first_seen_at, last_seen_at,
                       duration_ms, file_count, excluded_count, total_bytes, total_lines
                FROM snapshots
                WHERE root_path = ?
                ORDER BY last_seen_at DESC
                LIMIT ?
                """,
                (str(root.expanduser().resolve()), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def diff_snapshots(self, previous_snapshot_id: str, current_snapshot_id: str) -> dict[str, Any]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT p.path AS previous_path, p.sha256 AS previous_sha256,
                       c.path AS current_path, c.sha256 AS current_sha256
                FROM files p
                LEFT JOIN files c
                  ON c.snapshot_id = ? AND c.path = p.path
                WHERE p.snapshot_id = ?
                UNION ALL
                SELECT NULL, NULL, c.path, c.sha256
                FROM files c
                LEFT JOIN files p
                  ON p.snapshot_id = ? AND p.path = c.path
                WHERE c.snapshot_id = ? AND p.path IS NULL
                """,
                (
                    current_snapshot_id,
                    previous_snapshot_id,
                    previous_snapshot_id,
                    current_snapshot_id,
                ),
            ).fetchall()
        added: list[str] = []
        removed: list[str] = []
        changed: list[str] = []
        unchanged = 0
        for row in rows:
            previous_path = row["previous_path"]
            current_path = row["current_path"]
            if previous_path is None:
                added.append(str(current_path))
            elif current_path is None:
                removed.append(str(previous_path))
            elif row["previous_sha256"] != row["current_sha256"]:
                changed.append(str(current_path))
            else:
                unchanged += 1
        return {
            "previous_snapshot_id": previous_snapshot_id,
            "current_snapshot_id": current_snapshot_id,
            "added": sorted(added),
            "removed": sorted(removed),
            "changed": sorted(changed),
            "unchanged_count": unchanged,
        }

    def project_stale_claims(
        self, previous_snapshot_id: str, current_snapshot_id: str
    ) -> list[dict[str, Any]]:
        """Project old claims as stale without rewriting their historical truth."""

        difference = self.diff_snapshots(previous_snapshot_id, current_snapshot_id)
        changed_paths = set(difference["added"] + difference["removed"] + difference["changed"])
        file_set_changed = bool(difference["added"] or difference["removed"])
        python_graph_changed = any(path.endswith(".py") for path in changed_paths)
        now = utc_now()
        stale: list[dict[str, Any]] = []
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT c.claim_id, c.claim, k.invalidation_key
                FROM claims c
                JOIN invalidation_keys k ON k.claim_id = c.claim_id
                WHERE c.snapshot_id = ?
                ORDER BY c.claim_id, k.invalidation_key
                """,
                (previous_snapshot_id,),
            ).fetchall()
            reasons_by_claim: dict[str, set[str]] = {}
            claim_text: dict[str, str] = {}
            for row in rows:
                key = str(row["invalidation_key"])
                reason: str | None = None
                if key.startswith("file:") and key.removeprefix("file:") in changed_paths:
                    reason = f"changed dependency {key}"
                elif key == "snapshot:file-set" and file_set_changed:
                    reason = "repository file set changed"
                elif key == "python:import-graph" and python_graph_changed:
                    reason = "Python import graph may have changed"
                elif key.startswith("module:") and python_graph_changed:
                    reason = f"module relationship may have changed: {key}"
                if reason:
                    reasons_by_claim.setdefault(str(row["claim_id"]), set()).add(reason)
                    claim_text[str(row["claim_id"])] = str(row["claim"])

            for claim_id, reasons in reasons_by_claim.items():
                reason = "; ".join(sorted(reasons))
                connection.execute(
                    """
                    INSERT OR REPLACE INTO claim_validity(
                        claim_id, evaluated_against_snapshot_id, status, reason, created_at
                    ) VALUES (?, ?, 'stale', ?, ?)
                    """,
                    (claim_id, current_snapshot_id, reason, now),
                )
                stale.append(
                    {"claim_id": claim_id, "claim": claim_text[claim_id], "reason": reason}
                )
        return sorted(stale, key=lambda item: item["claim_id"])

    def stale_claims(self, evaluated_against_snapshot_id: str) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT c.claim_id, c.snapshot_id, c.claim, c.category, c.importance,
                       v.status, v.reason, v.created_at
                FROM claim_validity v
                JOIN claims c ON c.claim_id = v.claim_id
                WHERE v.evaluated_against_snapshot_id = ? AND v.status = 'stale'
                ORDER BY c.category, c.claim
                """,
                (evaluated_against_snapshot_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def grouped_counts(self, snapshot_id: str, column: str) -> list[tuple[str, int]]:
        if column not in {"language", "role"}:
            raise ValueError(f"Unsupported grouping column: {column}")
        with self._session() as connection:
            rows = connection.execute(
                f"""
                SELECT {column} AS label, COUNT(*) AS count
                FROM files
                WHERE snapshot_id = ?
                GROUP BY {column}
                ORDER BY count DESC, label ASC
                """,
                (snapshot_id,),
            ).fetchall()
        return [(str(row["label"]), int(row["count"])) for row in rows]

    def save_analysis(self, result: AnalysisResult) -> str:
        self.initialize()
        run_id = stable_id(
            "analysis-run",
            (result.snapshot_id, result.analyzer_version, result.created_at),
        )
        with self._session() as connection:
            snapshot_exists = connection.execute(
                "SELECT 1 FROM snapshots WHERE snapshot_id = ?", (result.snapshot_id,)
            ).fetchone()
            if snapshot_exists is None:
                raise ValueError(f"Snapshot must be saved before analysis: {result.snapshot_id}")

            connection.executemany(
                """
                INSERT OR REPLACE INTO evidence(
                    evidence_id, snapshot_id, path, start_line, end_line, symbol,
                    evidence_kind, excerpt_sha256, analyzer, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        item.evidence_id,
                        item.snapshot_id,
                        item.path,
                        item.start_line,
                        item.end_line,
                        item.symbol,
                        item.evidence_kind,
                        item.excerpt_sha256,
                        item.analyzer,
                        item.created_at,
                    )
                    for item in result.evidence
                ),
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO symbols(
                    symbol_id, snapshot_id, path, qualified_name, kind, start_line,
                    end_line, language, analyzer, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        item.symbol_id,
                        item.snapshot_id,
                        item.path,
                        item.qualified_name,
                        item.kind,
                        item.start_line,
                        item.end_line,
                        item.language,
                        item.analyzer,
                        json.dumps(item.metadata, sort_keys=True, separators=(",", ":")),
                    )
                    for item in result.symbols
                ),
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO edges(
                    edge_id, snapshot_id, source_symbol_id, source_path, relationship,
                    target_ref, target_symbol_id, evidence_id, analyzer
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        item.edge_id,
                        item.snapshot_id,
                        item.source_symbol_id,
                        item.source_path,
                        item.relationship,
                        item.target_ref,
                        item.target_symbol_id,
                        item.evidence_id,
                        item.analyzer,
                    )
                    for item in result.edges
                ),
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO claims(
                    claim_id, snapshot_id, claim, category, status, confidence,
                    importance, produced_by, created_at, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        item.claim_id,
                        item.snapshot_id,
                        item.claim,
                        item.category,
                        item.status,
                        item.confidence,
                        item.importance,
                        item.produced_by,
                        item.created_at,
                        item.verified_at,
                    )
                    for item in result.claims
                ),
            )

            for claim in result.claims:
                connection.execute(
                    "DELETE FROM claim_evidence WHERE claim_id = ?", (claim.claim_id,)
                )
                connection.executemany(
                    """
                    INSERT INTO claim_evidence(claim_id, evidence_id, relationship)
                    VALUES (?, ?, ?)
                    """,
                    (
                        (claim.claim_id, evidence_id, "supports")
                        for evidence_id in claim.supporting_evidence
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO claim_evidence(claim_id, evidence_id, relationship)
                    VALUES (?, ?, ?)
                    """,
                    (
                        (claim.claim_id, evidence_id, "contradicts")
                        for evidence_id in claim.contradicting_evidence
                    ),
                )
                connection.execute(
                    "DELETE FROM invalidation_keys WHERE claim_id = ?", (claim.claim_id,)
                )
                connection.executemany(
                    "INSERT INTO invalidation_keys(claim_id, invalidation_key) VALUES (?, ?)",
                    ((claim.claim_id, key) for key in claim.invalidation_keys),
                )
                connection.execute(
                    "DELETE FROM claim_alternatives WHERE claim_id = ?", (claim.claim_id,)
                )
                connection.executemany(
                    "INSERT INTO claim_alternatives(claim_id, alternative) VALUES (?, ?)",
                    ((claim.claim_id, value) for value in claim.alternative_hypotheses),
                )

            connection.execute(
                """
                INSERT INTO analysis_runs(
                    run_id, snapshot_id, analyzer_version, created_at, duration_ms,
                    symbol_count, edge_count, evidence_count, claim_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.snapshot_id,
                    result.analyzer_version,
                    result.created_at,
                    result.duration_ms,
                    len(result.symbols),
                    len(result.edges),
                    len(result.evidence),
                    len(result.claims),
                ),
            )
            connection.executemany(
                """
                INSERT INTO analysis_coverage(
                    run_id, analyzer, language, eligible_files, analyzed_files,
                    failed_files, unsupported_files, claimed_files, failures_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        run_id,
                        item.analyzer,
                        item.language,
                        item.eligible_files,
                        item.analyzed_files,
                        item.failed_files,
                        item.unsupported_files,
                        item.claimed_files,
                        json.dumps(item.failures, ensure_ascii=False),
                    )
                    for item in result.coverage
                ),
            )

            fts_status = connection.execute(
                "SELECT value FROM metadata WHERE key = 'fts5'"
            ).fetchone()
            if fts_status is not None and fts_status["value"] == "enabled":
                connection.execute(
                    "DELETE FROM claim_search WHERE snapshot_id = ?", (result.snapshot_id,)
                )
                connection.executemany(
                    """
                    INSERT INTO claim_search(snapshot_id, claim_id, claim, category, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            claim.snapshot_id,
                            claim.claim_id,
                            claim.claim,
                            claim.category,
                            claim.status,
                        )
                        for claim in result.claims
                    ),
                )
        return run_id

    def latest_analysis(self, snapshot_id: str | None = None) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        self.initialize()
        query = """
            SELECT run_id, snapshot_id, analyzer_version, created_at, duration_ms,
                   symbol_count, edge_count, evidence_count, claim_count
            FROM analysis_runs
        """
        parameters: tuple[object, ...] = ()
        if snapshot_id is not None:
            query += " WHERE snapshot_id = ?"
            parameters = (snapshot_id,)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self._session() as connection:
            row = connection.execute(query, parameters).fetchone()
        return dict(row) if row is not None else None

    def analysis_coverage(self, snapshot_id: str) -> list[dict[str, Any]]:
        """Return analyzer coverage for the newest run of one snapshot."""

        latest = self.latest_analysis(snapshot_id)
        if latest is None:
            return []
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT analyzer, language, eligible_files, analyzed_files, failed_files,
                       unsupported_files, claimed_files, failures_json
                FROM analysis_coverage
                WHERE run_id = ?
                ORDER BY analyzer, language
                """,
                (latest["run_id"],),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["failures"] = json.loads(item.pop("failures_json"))
            eligible = int(item["eligible_files"])
            analyzed = int(item["analyzed_files"])
            item["coverage_ratio"] = analyzed / eligible if eligible else 1.0
            claimed = item["claimed_files"]
            item["yield_ratio"] = (
                int(claimed) / analyzed if claimed is not None and analyzed else None
            )
            results.append(item)
        return results

    def list_claims(
        self,
        snapshot_id: str,
        *,
        status: str | None = None,
        category: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 5_000:
            raise ValueError("Claim limit must be between 1 and 5000")
        if offset < 0:
            raise ValueError("Claim offset must not be negative")
        clauses = ["snapshot_id = ?"]
        parameters: list[object] = [snapshot_id]
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if category is not None:
            clauses.append("category = ?")
            parameters.append(category)
        parameters.append(limit)
        parameters.append(offset)
        with self._session() as connection:
            rows = connection.execute(
                f"""
                SELECT claim_id, snapshot_id, claim, category, status, confidence,
                       importance, produced_by, created_at, verified_at
                FROM claims
                WHERE {" AND ".join(clauses)}
                ORDER BY
                    CASE importance
                        WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2 ELSE 3
                    END,
                    category,
                    claim,
                    claim_id
                LIMIT ? OFFSET ?
                """,
                tuple(parameters),
            ).fetchall()
            results = [dict(row) for row in rows]
            for result in results:
                evidence_rows = connection.execute(
                    """
                    SELECT evidence_id, relationship FROM claim_evidence
                    WHERE claim_id = ? ORDER BY relationship, evidence_id
                    """,
                    (result["claim_id"],),
                ).fetchall()
                result["supporting_evidence"] = [
                    row["evidence_id"] for row in evidence_rows if row["relationship"] == "supports"
                ]
                result["contradicting_evidence"] = [
                    row["evidence_id"]
                    for row in evidence_rows
                    if row["relationship"] == "contradicts"
                ]
        return results

    def claims_by_ids(
        self,
        snapshot_id: str,
        claim_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Return exact claims in caller order, including both evidence sides.

        Context assembly used to read the first 5,000 claims into a lookup and
        silently lose receipts for any search result beyond that page. Large
        repositories are exactly where bounded context matters most, so an
        exact-ID lookup must not depend on the claim's position in a global
        listing.
        """

        ordered_ids = tuple(dict.fromkeys(str(item) for item in claim_ids if str(item)))
        if not ordered_ids:
            return []
        if len(ordered_ids) > 500:
            raise ValueError("Exact claim lookup accepts at most 500 IDs")
        placeholders = ", ".join("?" for _ in ordered_ids)
        with self._session() as connection:
            rows = connection.execute(
                f"""
                SELECT claim_id, snapshot_id, claim, category, status, confidence,
                       importance, produced_by, created_at, verified_at
                FROM claims
                WHERE snapshot_id = ? AND claim_id IN ({placeholders})
                """,
                (snapshot_id, *ordered_ids),
            ).fetchall()
            by_id = {str(row["claim_id"]): dict(row) for row in rows}
            for claim_id, result in by_id.items():
                evidence_rows = connection.execute(
                    """
                    SELECT evidence_id, relationship FROM claim_evidence
                    WHERE claim_id = ? ORDER BY relationship, evidence_id
                    """,
                    (claim_id,),
                ).fetchall()
                result["supporting_evidence"] = [
                    row["evidence_id"] for row in evidence_rows if row["relationship"] == "supports"
                ]
                result["contradicting_evidence"] = [
                    row["evidence_id"]
                    for row in evidence_rows
                    if row["relationship"] == "contradicts"
                ]
        return [by_id[claim_id] for claim_id in ordered_ids if claim_id in by_id]

    def list_files(self, snapshot_id: str) -> list[dict[str, Any]]:
        """Return every included file of one snapshot, ordered by path."""

        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT path, sha256, size_bytes, line_count, language, role
                FROM files WHERE snapshot_id = ? ORDER BY path
                """,
                (snapshot_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_rows(self, snapshot_id: str, table: str) -> int:
        """How many rows a snapshot actually holds in one table.

        A projection that reads a capped page cannot tell whether it saw
        everything, and the specification builder spent a long time reporting
        its own page size as the ledger's contents. Counting is the only way to
        check a projection against the thing it projects, so this exists to be
        compared against rather than to be read.
        """

        if table not in {"claims", "symbols", "evidence", "edges", "files"}:
            raise ValueError(f"Unsupported table for counting: {table}")
        with self._session() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS total FROM {table} WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        return int(row["total"]) if row else 0

    def list_exclusions(self, snapshot_id: str) -> list[dict[str, Any]]:
        """Return every entry the scan policy excluded, with its reason."""

        with self._session() as connection:
            rows = connection.execute(
                "SELECT path, reason, contained_files FROM exclusions "
                "WHERE snapshot_id = ? ORDER BY path",
                (snapshot_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_edges(
        self,
        snapshot_id: str,
        *,
        relationships: Sequence[str] | None = None,
        limit: int = 20_000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return relationship edges, optionally filtered to specific relationships."""

        if limit < 1 or limit > 200_000:
            raise ValueError("Edge limit must be between 1 and 200000")
        if offset < 0:
            raise ValueError("Edge offset must not be negative")
        clauses = ["snapshot_id = ?"]
        parameters: list[object] = [snapshot_id]
        if relationships:
            placeholders = ", ".join("?" for _ in relationships)
            clauses.append(f"relationship IN ({placeholders})")
            parameters.extend(relationships)
        parameters.append(limit)
        parameters.append(offset)
        with self._session() as connection:
            rows = connection.execute(
                f"""
                SELECT edge_id, source_symbol_id, source_path, relationship, target_ref,
                       target_symbol_id, evidence_id, analyzer
                FROM edges
                WHERE {" AND ".join(clauses)}
                ORDER BY relationship, source_path, target_ref, edge_id
                LIMIT ? OFFSET ?
                """,
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_evidence(self, snapshot_id: str) -> list[dict[str, Any]]:
        """Return every evidence receipt of one snapshot."""

        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT e.evidence_id, e.snapshot_id, e.path, e.start_line, e.end_line,
                       e.symbol, e.evidence_kind, e.excerpt_sha256, e.analyzer,
                       e.created_at, f.sha256 AS file_sha256
                FROM evidence e
                LEFT JOIN files f
                    ON f.snapshot_id = e.snapshot_id AND f.path = e.path
                WHERE e.snapshot_id = ?
                ORDER BY e.path, e.start_line, e.evidence_id
                """,
                (snapshot_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT evidence_id, snapshot_id, path, start_line, end_line, symbol,
                       evidence_kind, excerpt_sha256, analyzer, created_at
                FROM evidence WHERE evidence_id = ?
                """,
                (evidence_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_evidence_excerpt(
        self, evidence_id: str, *, max_lines: int = 80
    ) -> dict[str, Any] | None:
        if max_lines < 1 or max_lines > 500:
            raise ValueError("Evidence excerpt max_lines must be between 1 and 500")
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT e.evidence_id, e.snapshot_id, e.path, e.start_line, e.end_line,
                       e.symbol, e.evidence_kind, e.excerpt_sha256, e.analyzer,
                       s.root_path, f.sha256 AS file_sha256
                FROM evidence e
                JOIN snapshots s ON s.snapshot_id = e.snapshot_id
                LEFT JOIN files f ON f.snapshot_id = e.snapshot_id AND f.path = e.path
                WHERE e.evidence_id = ?
                """,
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        path = str(result["path"])
        if path == "." or path.startswith("@") or result["file_sha256"] is None:
            result["excerpt"] = None
            result["excerpt_status"] = "virtual-evidence"
            return result
        root = Path(str(result["root_path"])).expanduser().resolve()
        source = (root / Path(path)).resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise ValueError("Evidence path escapes its pinned repository root") from exc
        payload = source.read_bytes()
        current_hash = hashlib.sha256(payload).hexdigest()
        if current_hash != result["file_sha256"]:
            result["excerpt"] = None
            result["excerpt_status"] = "source-changed-after-snapshot"
            return result
        lines = payload.decode("utf-8", errors="strict").splitlines(keepends=True)
        start = int(result["start_line"] or 1)
        stored_end = int(result["end_line"] or start)
        pinned_excerpt_hash = hashlib.sha256(
            "".join(lines[start - 1 : stored_end]).encode("utf-8")
        ).hexdigest()
        if result["excerpt_sha256"] and pinned_excerpt_hash != result["excerpt_sha256"]:
            result["excerpt"] = None
            result["excerpt_status"] = "receipt-excerpt-hash-mismatch"
            return result
        end = min(stored_end, start + max_lines - 1)
        result["excerpt"] = "".join(lines[start - 1 : end])
        result["excerpt_start_line"] = start
        result["excerpt_end_line"] = end
        result["excerpt_truncated"] = end < stored_end
        result["excerpt_status"] = "verified-current-by-file-hash"
        return result

    def list_symbols(
        self,
        snapshot_id: str,
        *,
        query: str | None = None,
        kind: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 5_000:
            raise ValueError("Symbol limit must be between 1 and 5000")
        if offset < 0:
            raise ValueError("Symbol offset must not be negative")
        clauses = ["snapshot_id = ?"]
        parameters: list[object] = [snapshot_id]
        if query:
            clauses.append("qualified_name LIKE ?")
            parameters.append(f"%{query}%")
        if kind:
            clauses.append("kind = ?")
            parameters.append(kind)
        parameters.append(limit)
        parameters.append(offset)
        with self._session() as connection:
            rows = connection.execute(
                f"""
                SELECT symbol_id, snapshot_id, path, qualified_name, kind, start_line,
                       end_line, language, analyzer, metadata_json
                FROM symbols
                WHERE {" AND ".join(clauses)}
                ORDER BY qualified_name, path, start_line, symbol_id
                LIMIT ? OFFSET ?
                """,
                tuple(parameters),
            ).fetchall()
        results = [dict(row) for row in rows]
        for result in results:
            result["metadata"] = json.loads(result.pop("metadata_json"))
        return results

    def symbol_neighbors(
        self, snapshot_id: str, symbol_id: str, *, limit: int = 300
    ) -> dict[str, Any]:
        if limit < 1 or limit > 2_000:
            raise ValueError("Neighbor limit must be between 1 and 2000")
        with self._session() as connection:
            symbol = connection.execute(
                """
                SELECT symbol_id, path, qualified_name, kind, start_line, end_line,
                       language, analyzer, metadata_json
                FROM symbols WHERE snapshot_id = ? AND symbol_id = ?
                """,
                (snapshot_id, symbol_id),
            ).fetchone()
            if symbol is None:
                raise ValueError(f"Symbol not found in snapshot: {symbol_id}")
            rows = connection.execute(
                """
                SELECT edge_id, source_symbol_id, source_path, relationship, target_ref,
                       target_symbol_id, evidence_id, analyzer,
                       CASE WHEN source_symbol_id = ? THEN 'outgoing' ELSE 'incoming' END AS direction
                FROM edges
                WHERE snapshot_id = ? AND (source_symbol_id = ? OR target_symbol_id = ?)
                ORDER BY direction, relationship, target_ref
                LIMIT ?
                """,
                (symbol_id, snapshot_id, symbol_id, symbol_id, limit),
            ).fetchall()
        symbol_result = dict(symbol)
        symbol_result["metadata"] = json.loads(symbol_result.pop("metadata_json"))
        return {"symbol": symbol_result, "edges": [dict(row) for row in rows]}

    def context_pack(
        self,
        snapshot_id: str,
        query: str,
        *,
        max_chars: int = 20_000,
        max_claims: int = 12,
    ) -> dict[str, Any]:
        if max_chars < 1_000 or max_chars > 200_000:
            raise ValueError("Context pack max_chars must be between 1000 and 200000")
        if max_claims < 1 or max_claims > 100:
            raise ValueError("Context pack max_claims must be between 1 and 100")
        matches = self.search_claims(snapshot_id, query, limit=max_claims)
        claims = self.claims_by_ids(
            snapshot_id,
            [str(item["claim_id"]) for item in matches],
        )
        return self._bounded_context_pack(
            snapshot_id,
            query,
            claims,
            max_chars=max_chars,
            omitted_claim_ids=(),
            missing_claim_ids=(),
        )

    def context_pack_for_claims(
        self,
        snapshot_id: str,
        claim_ids: Sequence[str],
        *,
        query: str,
        max_chars: int = 20_000,
        max_claims: int = 100,
    ) -> dict[str, Any]:
        """Build a bounded pack for an exact evidence obligation.

        Search is right for an interactive question. A synthesis job already
        knows which claims its section is responsible for, and searching their
        prose again can substitute a similarly worded claim or miss one whose
        vocabulary differs. Exact selection keeps the narrative packet aligned
        with the deterministic outline routing.
        """

        if not query.strip():
            raise ValueError("Context pack query cannot be empty")
        if max_chars < 1_000 or max_chars > 200_000:
            raise ValueError("Context pack max_chars must be between 1000 and 200000")
        if max_claims < 1 or max_claims > 100:
            raise ValueError("Context pack max_claims must be between 1 and 100")
        requested = tuple(dict.fromkeys(str(item) for item in claim_ids if str(item)))
        bounded_ids = requested[:max_claims]
        claims = self.claims_by_ids(snapshot_id, bounded_ids)
        found_ids = {str(item["claim_id"]) for item in claims}
        return self._bounded_context_pack(
            snapshot_id,
            query,
            claims,
            max_chars=max_chars,
            omitted_claim_ids=requested[max_claims:],
            missing_claim_ids=tuple(item for item in bounded_ids if item not in found_ids),
        )

    def _bounded_context_pack(
        self,
        snapshot_id: str,
        query: str,
        claims: Sequence[dict[str, Any]],
        *,
        max_chars: int,
        omitted_claim_ids: Sequence[str],
        missing_claim_ids: Sequence[str],
    ) -> dict[str, Any]:
        """Fit exact claims and two-sided receipts into one provider packet."""

        selected: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        omitted_claims = list(omitted_claim_ids)
        omitted_evidence: list[str] = []
        used_chars = 0
        for claim in claims:
            claim_chars = len(str(claim.get("claim", "")))
            if used_chars + claim_chars > max_chars:
                omitted_claims.append(str(claim["claim_id"]))
                continue
            selected.append(claim)
            used_chars += claim_chars
            evidence_sides = (
                ("supports", claim.get("supporting_evidence", [])),
                ("contradicts", claim.get("contradicting_evidence", [])),
            )
            for relationship, evidence_ids in evidence_sides:
                for evidence_id in evidence_ids[:8]:
                    receipt = self.get_evidence_excerpt(str(evidence_id), max_lines=12)
                    if receipt is None:
                        omitted_evidence.append(str(evidence_id))
                        continue
                    excerpt_chars = len(str(receipt.get("excerpt") or ""))
                    if used_chars + excerpt_chars > max_chars:
                        omitted_evidence.append(str(evidence_id))
                        continue
                    receipts.append({**receipt, "relationship": relationship})
                    used_chars += excerpt_chars
        return {
            "snapshot_id": snapshot_id,
            "query": query,
            "max_chars": max_chars,
            "used_chars": used_chars,
            "claims": selected,
            "evidence": receipts,
            "omitted_claim_ids": omitted_claims,
            "omitted_evidence_ids": omitted_evidence,
            "missing_claim_ids": list(missing_claim_ids),
            "truncated": bool(omitted_claims or omitted_evidence or missing_claim_ids),
        }

    def search_claims(
        self,
        snapshot_id: str,
        query: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            raise ValueError("Search query cannot be empty")
        if limit < 1 or limit > 500:
            raise ValueError("Search limit must be between 1 and 500")
        with self._session() as connection:
            fts_status = connection.execute(
                "SELECT value FROM metadata WHERE key = 'fts5'"
            ).fetchone()
            if fts_status is not None and fts_status["value"] == "enabled":
                try:
                    rows = connection.execute(
                        """
                        SELECT c.claim_id, c.snapshot_id, c.claim, c.category, c.status,
                               c.confidence, c.importance, c.produced_by
                        FROM claim_search s
                        JOIN claims c ON c.claim_id = s.claim_id
                        WHERE s.snapshot_id = ? AND claim_search MATCH ?
                        ORDER BY bm25(claim_search), c.claim
                        LIMIT ?
                        """,
                        (snapshot_id, query, limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = connection.execute(
                        """
                        SELECT claim_id, snapshot_id, claim, category, status,
                               confidence, importance, produced_by
                        FROM claims
                        WHERE snapshot_id = ? AND claim LIKE ?
                        ORDER BY claim LIMIT ?
                        """,
                        (snapshot_id, f"%{query}%", limit),
                    ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT claim_id, snapshot_id, claim, category, status,
                           confidence, importance, produced_by
                    FROM claims
                    WHERE snapshot_id = ? AND claim LIKE ?
                    ORDER BY claim LIMIT ?
                    """,
                    (snapshot_id, f"%{query}%", limit),
                ).fetchall()
        return [dict(row) for row in rows]
