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

import hashlib
import json
import sqlite3
import zlib
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path
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


# What a stored entry was written by. The reader's own version is already in
# every key, so this covers the rest: the shape of what is written down, which
# can change without any reader changing. A store written under another one is
# dropped rather than read, because a cache that cannot be trusted completely
# is not a cache.
STORE_VERSION = "open-skeleton.read-cache.v2"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS read_cache (
    key_sha256 TEXT PRIMARY KEY,
    reader TEXT NOT NULL,
    path TEXT NOT NULL,
    key_json TEXT NOT NULL,
    outcome_deflated BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS read_cache_path_idx ON read_cache(reader, path);
"""


def key_digest(key: CacheKey) -> str:
    """One column standing for a key of any length, terminated field by field."""

    return hashlib.sha256("\x00".join([*key, ""]).encode("utf-8")).hexdigest()


def record_to_json(record: Any) -> dict[str, Any]:
    """A record as JSON types. Tuples become lists, which is what JSON has."""

    return asdict(record)


def record_from_json(cls: type[Any], data: dict[str, Any]) -> Any:
    """A record rebuilt from JSON, with sequence types restored.

    Which fields to restore is read from the dataclass rather than listed
    here, so a field added later is handled instead of quietly coming back as
    a list. `from __future__ import annotations` makes every annotation a
    string, which is why these are compared as text.
    """

    values = dict(data)
    for field_ in dataclass_fields(cls):
        if field_.name not in values or values[field_.name] is None:
            continue
        annotation = str(field_.type)
        if annotation.startswith("tuple"):
            values[field_.name] = tuple(values[field_.name])
        elif annotation.startswith("frozenset"):
            values[field_.name] = frozenset(values[field_.name])
    return cls(**values)


def load(path: Path, decode: Callable[[dict[str, Any]], Any]) -> ReadCache:
    """Every entry a previous run wrote, or an empty cache.

    Anything unreadable is an empty cache rather than an error. A cache is an
    optimisation, and a corrupt one must cost a slow run, never a failed
    analysis or -- far worse -- a wrong one: an entry that will not decode is
    dropped, not guessed at.
    """

    cache = ReadCache()
    if not path.exists():
        return cache
    try:
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            stored = connection.execute(
                "SELECT value FROM metadata WHERE key = 'store_version'"
            ).fetchone()
            if stored is None or str(stored["value"]) != STORE_VERSION:
                return cache
            rows = connection.execute(
                "SELECT key_json, outcome_deflated FROM read_cache"
            ).fetchall()
    except (sqlite3.Error, OSError):
        return cache

    for row in rows:
        try:
            key = tuple(json.loads(row["key_json"]))
            payload = zlib.decompress(row["outcome_deflated"]).decode("utf-8")
            cache.entries[key] = decode(json.loads(payload))
        except (ValueError, TypeError, KeyError, zlib.error):
            continue
    return cache


def save(path: Path, cache: ReadCache, encode: Callable[[Any], dict[str, Any]]) -> int:
    """Write the entries this store does not already hold, and return how many.

    Only what is missing, because rewriting every entry costs about as much as
    encoding the whole repository again -- 2.1 s on mypy, measured in
    docs/PERFORMANCE.md -- and almost all of it would be identical. A failure
    to write is not raised for the same reason a failure to read is not: the
    analysis it came from is already correct.
    """

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('store_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (STORE_VERSION,),
            )
            held = {str(row[0]) for row in connection.execute("SELECT key_sha256 FROM read_cache")}
            written = 0
            for key, value in cache.entries.items():
                digest = key_digest(key)
                if digest in held:
                    continue
                connection.execute(
                    "INSERT OR REPLACE INTO read_cache"
                    "(key_sha256, reader, path, key_json, outcome_deflated) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        digest,
                        key[0] if key else "",
                        key[1] if len(key) > 1 else "",
                        json.dumps(list(key)),
                        # Level 1: this is written on a path a person is
                        # waiting on, and the cheapest setting already turns
                        # mypy's store from 107 MB into 29. Anything slower
                        # spends the run's time to save disk nobody is short
                        # of.
                        zlib.compress(
                            json.dumps(encode(value), separators=(",", ":")).encode("utf-8"), 1
                        ),
                    ),
                )
                written += 1
            connection.commit()
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return 0
    return written


def forget_absent(path: Path, keep: set[str]) -> int:
    """Drop entries for files the snapshot no longer holds, and count them.

    Without this the store grows with every file the repository ever had. It
    is keyed on the path rather than the whole key on purpose: several entries
    for one path are versions of it, and a path that is gone has no versions
    worth keeping.
    """

    try:
        with closing(sqlite3.connect(path)) as connection:
            rows = connection.execute("SELECT DISTINCT path FROM read_cache").fetchall()
            gone = [str(row[0]) for row in rows if str(row[0]) not in keep]
            connection.executemany("DELETE FROM read_cache WHERE path = ?", ((p,) for p in gone))
            connection.commit()
    except (sqlite3.Error, OSError):
        return 0
    return len(gone)
