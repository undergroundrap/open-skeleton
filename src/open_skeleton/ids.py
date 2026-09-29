# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""How a fact is named.

Two rules, and the second exists to keep the first safe.

A fact read from one file is named after the bytes it was read from, never
after the snapshot it was read during. A snapshot id digests every file's
content hash, so naming facts after it gives the whole repository new names
whenever one line changes, and nothing computed for one snapshot can be
matched to anything in the next. That is the single reason re-analysis cannot
reuse anything, and `content_key` is how a reader avoids it.

The rule the snapshot id was quietly providing has to be kept, though. Because
it moved whenever any file moved, it also guaranteed that a receipt could not
outlive the bytes it quotes. Take it out of an identifier and put nothing in
its place, and two different versions of a line mint the same receipt id: the
ledger serves the old excerpt as evidence for the new file, and no check
downstream can notice, because as far as any of them can tell the receipt
still verifies. So a per-file identifier must include `content_key(...)`, and
`tests/test_fact_identity.py` holds every reader to both halves.

A fact about the whole snapshot -- a census, whose path is `.` and whose
excerpt is the file inventory itself -- is still named after the snapshot,
because that is what it is a fact about.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from open_skeleton.models import Snapshot

# Path-to-content-hash tables, by snapshot id. Keying the memo on the snapshot
# id is exact rather than approximate: that id is a digest of every file's
# path, content hash, size, language and role, so two snapshots sharing one
# cannot disagree about any entry in this table.
#
# Bounded because a long-running MCP server analyzes many snapshots in one
# process and an unbounded memo would hold every file list it had ever seen.
# Analysis reads one snapshot at a time, so a small number of entries is the
# difference between always hitting and occasionally rebuilding a dictionary.
_CONTENT_HASHES: dict[str, dict[str, str]] = {}
_MEMO_LIMIT = 4


def stable_id(namespace: str, values: Iterable[object]) -> str:
    # Every field is terminated by a NUL, the namespace included. The bytes are
    # joined and hashed once rather than fed in field by field: identifiers are
    # persisted, so the digest must not change, and it does not -- UTF-8 of a
    # concatenation is the concatenation of the UTF-8 -- but one `update` per
    # record instead of one per field is most of what minting an id cost.
    fields = [namespace, *map(str, values), ""]
    return hashlib.sha256("\x00".join(fields).encode("utf-8")).hexdigest()


def content_key(snapshot: Snapshot, path: str) -> str:
    """The bytes a fact read from `path` should be named after.

    An unknown path returns the empty string rather than raising. A reader can
    legitimately hold a path the snapshot does not -- the Hum adapter reads an
    index file from outside the approved root -- and those call sites supply
    their own digest instead. Raising here would turn a reader's normal case
    into an analysis failure.
    """

    table = _CONTENT_HASHES.get(snapshot.snapshot_id)
    if table is None:
        if len(_CONTENT_HASHES) >= _MEMO_LIMIT:
            _CONTENT_HASHES.clear()
        table = {item.path: item.sha256 for item in snapshot.files}
        _CONTENT_HASHES[snapshot.snapshot_id] = table
    return table.get(path, "")
