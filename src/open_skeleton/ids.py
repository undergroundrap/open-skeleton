# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
from collections.abc import Iterable


def stable_id(namespace: str, values: Iterable[object]) -> str:
    digest = hashlib.sha256()
    digest.update(namespace.encode("utf-8"))
    digest.update(b"\x00")
    for value in values:
        digest.update(str(value).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()
