# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
from collections.abc import Iterable


def stable_id(namespace: str, values: Iterable[object]) -> str:
    # Every field is terminated by a NUL, the namespace included. The bytes are
    # joined and hashed once rather than fed in field by field: identifiers are
    # persisted, so the digest must not change, and it does not -- UTF-8 of a
    # concatenation is the concatenation of the UTF-8 -- but one `update` per
    # record instead of one per field is most of what minting an id cost.
    fields = [namespace, *map(str, values), ""]
    return hashlib.sha256("\x00".join(fields).encode("utf-8")).hexdigest()
