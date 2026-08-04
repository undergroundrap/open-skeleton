# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path


def local_state_home() -> Path:
    """Return the platform-local, non-roaming state home without creating it."""

    if os.name == "nt":
        configured = os.environ.get("LOCALAPPDATA")
        return Path(configured) if configured else Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    configured = os.environ.get("XDG_STATE_HOME")
    return Path(configured) if configured else Path.home() / ".local" / "state"


def default_state_dir(root: Path, *, state_home: Path | None = None) -> Path:
    """Map one resolved repository root to a stable external state directory."""

    approved_root = root.expanduser().resolve(strict=True)
    normalized = os.path.normcase(str(approved_root))
    root_hash = hashlib.sha256(os.fsencode(normalized)).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", approved_root.name.casefold()).strip("-")
    slug = (slug or "repository")[:48]
    base = (state_home or local_state_home()).expanduser().resolve()
    return base / "open-skeleton" / "state" / f"{slug}-{root_hash}"


def resolve_state_dir(root: Path, requested: Path | None) -> Path:
    """Resolve a state path and reject any write location inside the target root."""

    approved_root = root.expanduser().resolve(strict=True)
    state_dir = (
        requested.expanduser().resolve()
        if requested is not None
        else default_state_dir(approved_root)
    )
    if state_dir == approved_root or state_dir.is_relative_to(approved_root):
        raise ValueError("State directory must be outside the analyzed repository")
    return state_dir
