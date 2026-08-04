# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import codecs
import hashlib
import os
import time
from collections.abc import Callable
from pathlib import Path

from open_skeleton.models import ExclusionRecord, FileRecord, ScanEvent, Snapshot, utc_now
from open_skeleton.policy import POLICY_VERSION, ScanPolicy, classify_language, classify_role

EventCallback = Callable[[ScanEvent], None]
_CHUNK_SIZE = 64 * 1024


class UnsupportedTextEncodingError(ValueError):
    """Raised when a candidate text file is not valid UTF-8."""


class BinaryContentError(ValueError):
    """Raised when a candidate contains a NUL byte in its initial payload."""


def _inspect_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    line_count = 0
    byte_count = 0
    last_byte: int | None = None
    first_chunk = True

    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_SIZE):
                if first_chunk and b"\x00" in chunk[:8192]:
                    raise BinaryContentError("binary-content")
                first_chunk = False
                digest.update(chunk)
                byte_count += len(chunk)
                line_count += chunk.count(b"\n")
                last_byte = chunk[-1]
                decoder.decode(chunk, final=False)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise UnsupportedTextEncodingError("unsupported-text-encoding") from exc

    if byte_count > 0 and last_byte != ord("\n"):
        line_count += 1
    return digest.hexdigest(), line_count


def _snapshot_id(files: list[FileRecord]) -> str:
    digest = hashlib.sha256()
    digest.update(POLICY_VERSION.encode("utf-8"))
    digest.update(b"\x00")
    for item in sorted(files, key=lambda value: value.path):
        for value in (item.path, item.sha256, str(item.size_bytes), item.language, item.role):
            digest.update(value.encode("utf-8"))
            digest.update(b"\x00")
    return digest.hexdigest()


def scan_repository(
    root: Path,
    *,
    policy: ScanPolicy | None = None,
    on_event: EventCallback | None = None,
) -> Snapshot:
    """Create a deterministic, read-only inventory of regular UTF-8 text files."""

    started = time.perf_counter()
    created_at = utc_now()
    approved_root = root.expanduser().resolve(strict=True)
    if not approved_root.is_dir():
        raise ValueError(f"Repository root is not a directory: {approved_root}")

    active_policy = policy or ScanPolicy()
    files: list[FileRecord] = []
    exclusions: list[ExclusionRecord] = []
    events: list[ScanEvent] = []

    def emit(
        stage: str,
        message: str,
        *,
        path: str | None = None,
        processed_files: int | None = None,
    ) -> None:
        event = ScanEvent(
            stage=stage,
            message=message,
            path=path,
            processed_files=processed_files,
        )
        events.append(event)
        if on_event is not None:
            on_event(event)

    emit("starting", "Validated repository root")

    def exclude(relative_path: str, reason: str) -> None:
        exclusions.append(ExclusionRecord(path=relative_path, reason=reason))

    def visit(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError as exc:
            relative = directory.relative_to(approved_root).as_posix() or "."
            exclude(relative, f"directory-read-error:{exc.__class__.__name__}")
            return

        for entry in entries:
            candidate = Path(entry.path)
            relative = candidate.relative_to(approved_root).as_posix()
            try:
                if entry.is_symlink():
                    exclude(relative, "symlink-not-followed")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    reason = active_policy.directory_exclusion(entry.name)
                    if reason:
                        exclude(f"{relative}/", reason)
                    else:
                        visit(candidate)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    exclude(relative, "non-regular-file")
                    continue

                stat = entry.stat(follow_symlinks=False)
                reason = active_policy.file_exclusion(candidate, stat.st_size)
                if reason:
                    exclude(relative, reason)
                    continue

                try:
                    sha256, line_count = _inspect_file(candidate)
                except (BinaryContentError, UnsupportedTextEncodingError) as exc:
                    exclude(relative, str(exc))
                    continue
                except OSError as exc:
                    exclude(relative, f"file-read-error:{exc.__class__.__name__}")
                    continue

                files.append(
                    FileRecord(
                        path=relative,
                        sha256=sha256,
                        size_bytes=stat.st_size,
                        line_count=line_count,
                        language=classify_language(candidate),
                        role=classify_role(Path(relative)),
                    )
                )
                if len(files) == 1 or len(files) % 100 == 0:
                    emit(
                        "discovering",
                        f"Inventoried {len(files)} files",
                        path=relative,
                        processed_files=len(files),
                    )
            except OSError as exc:
                exclude(relative, f"metadata-read-error:{exc.__class__.__name__}")

    visit(approved_root)
    files.sort(key=lambda item: item.path)
    exclusions.sort(key=lambda item: (item.path, item.reason))
    snapshot_id = _snapshot_id(files)
    duration_ms = round((time.perf_counter() - started) * 1000)
    emit(
        "complete",
        f"Inventory complete: {len(files)} included, {len(exclusions)} excluded",
        processed_files=len(files),
    )

    return Snapshot(
        snapshot_id=snapshot_id,
        root=approved_root,
        policy_version=POLICY_VERSION,
        created_at=created_at,
        duration_ms=duration_ms,
        files=tuple(files),
        exclusions=tuple(exclusions),
        events=tuple(events),
    )
