# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Keeping what a reader already read, and only where that is provably safe.

A reader that runs over an unchanged file does the same work and reaches the
same answer, so the second run is waste. Skipping it is only correct if the
answer really did depend on nothing else, and a cached fact that should have
been invalidated is a wrong answer recorded as a fact -- which this engine
treats as worse than a slow one.

So the key is not "the file". The key is every input the reader was given.
`benchmarks/scaling/run_reader_locality.py` found what happens when that
distinction is skipped: removing a module changed a symbol in another file,
keeping the same identifier, because the metadata recording where each call
lands reclassified a call into the removed module from `this repository` to
`dependency`. The file's bytes had not changed. A cache keyed on the bytes
would have hit and served the old answer.

A key names the reader's whole input, so that class of mistake cannot be made
by forgetting a rule. For the Python reader that is its version, the file's
path and content hash, the module name the file was given, and a digest of
every module name in the snapshot -- the last being exactly the input that
reclassified the call above. Adding or removing any module changes that digest
and misses every entry, which is more conservative than it has to be: the
measurement says most of those answers would not have changed. Conservative is
the side to be wrong on, and editing a file, which is what an agent loop does
between turns, leaves the digest alone and hits everything.

Nothing here decides what is cacheable. A reader hands over a value it says is
a pure function of a key it names, and this stores it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# What a cached entry is filed under: a tuple of strings naming every input
# the reader was given. Strings rather than objects so an entry can be written
# somewhere other than this process without the key meaning something else.
CacheKey = tuple[str, ...]


@dataclass(slots=True)
class ReadCache:
    """Per-file reader outcomes, with a record of how often they were used.

    The counters are not decoration. A cache that silently stops hitting looks
    exactly like a cache that is working, and the only way to tell is to have
    the number to hand: `hits` and `misses` are what the benchmark reports and
    what a test asserts against, so "it re-read one file" is a measurement
    rather than an impression.
    """

    entries: dict[CacheKey, Any] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def get(self, key: CacheKey) -> Any | None:
        found = self.entries.get(key)
        if found is None:
            self.misses += 1
        else:
            self.hits += 1
        return found

    def put(self, key: CacheKey, value: Any) -> None:
        self.entries[key] = value

    def clear(self) -> None:
        self.entries.clear()
        self.hits = 0
        self.misses = 0

    @property
    def reads_avoided(self) -> int:
        return self.hits
