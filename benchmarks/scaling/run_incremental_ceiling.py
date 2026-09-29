# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""How much of a run survives a one-line edit?

A one-line edit re-reads the whole repository today, and the reason is
identity rather than effort: every evidence, symbol and edge identifier hashes
the snapshot id, and a snapshot id hashes every file's content hash. Change one
byte and every identifier in the repository is new, so nothing computed for the
previous snapshot can be matched to anything in this one.

That is a statement about how facts are named, not about whether they changed.
This instrument separates the two. It analyzes a repository, changes one line
in one file, analyzes it again, and asks of every record whether it is
byte-identical to one from the first run once the snapshot id is taken out of
its identity. Three answers are possible and each means something different:

- **reusable**: identical. A correct incremental run could have kept it.
- **from an edited file**: the record's path, or an invalidation key naming a
  path, is a file whose bytes changed. It has to be recomputed, and a cache
  that kept it would be wrong.
- **neither**: identical to nothing, and traceable to no file that changed.
  This is the class worth naming, because it is where a cache would be unsafe
  without anyone noticing. Every such record is printed.

Taking the snapshot id out of a record's identity cannot be done by editing
the text of a record, and the first version of this file tried. An identifier
is a digest of the fields that produced it, so a snapshot id that reached it
is not in the output to be replaced -- substitution normalised the one field
spelling it out, left every hash that had consumed it, and reported that
nothing at all was reusable. That is a measurement of the instrument.

A second version wrapped `stable_id` to drop the snapshot id before hashing,
simulating the change so the ceiling could be measured before the work. That
wrapper is gone: the readers now key per-file facts by `content_key`, so what
this reports is the reuse actually available rather than the reuse that would
be. Leaving the wrapper in would have been worse than useless, because it was
never selective -- it would have gone on content-keying the claims and census
receipts that are still named after their snapshot on purpose, and reported
them reusable when nothing reuses them.

Claims are expected near zero here, and that is the design rather than a gap.
A claim id hashes the snapshot, the category and the text but not the path, so
that two files stating the same thing merge into one claim carrying both
receipts. Roughly a thousand claims per repository are rewritten each run
against a few hundred thousand receipts that are not.

    python benchmarks/scaling/run_incremental_ceiling.py -- <repository> [...]
    python benchmarks/scaling/run_incremental_ceiling.py --edit src/thing.py -- <repository>

The repository is copied to a temporary directory before being edited; the
path given is never written to. Exit status is zero: this measures what is
reusable, it is not a gate.
"""

from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import open_skeleton
from open_skeleton import models

PINNED_CLOCK = "2026-01-01T00:00:00.000+00:00"

# Appended rather than inserted, so no earlier line moves and the measurement
# is not dominated by every receipt in one file shifting by a line. The point
# is what an edit does to the rest of the repository.
ADDED_LINE = "\n# open-skeleton incremental-ceiling measurement\n"

RECORD_CLASSES = ("evidence", "symbols", "edges", "claims")


def _pin_clock() -> None:
    """Every module's `utc_now`, pinned, so two runs differ only where the code does.

    Rebound module by module rather than once at the source, because every
    module here imports the name directly and rebinding `models.utc_now` alone
    would not reach those second references.

    Without this, `created_at` alone makes every evidence and claim record
    differ between two runs, and the measured reuse is zero for a reason that
    has nothing to do with the question being asked.
    """

    def pinned() -> str:
        return PINNED_CLOCK

    models.utc_now = pinned
    for info in pkgutil.walk_packages(open_skeleton.__path__, "open_skeleton."):
        module = importlib.import_module(info.name)
        if hasattr(module, "utc_now"):
            module.utc_now = pinned  # type: ignore[attr-defined]


_pin_clock()

from open_skeleton.analysis import analyze_snapshot  # noqa: E402
from open_skeleton.scanner import scan_repository  # noqa: E402


def canonical(record: Any) -> str:
    """A record's whole content, identifiers included, less the snapshot id.

    Every field is read from the dataclass rather than from a list kept here,
    so a field added to a record later is compared rather than silently
    ignored -- the failure mode where an instrument keeps reporting a number
    that stopped describing what it names.

    Identifiers need no normalising: `stable_id` is wrapped for this run, so
    they already follow content. Only the `snapshot_id` field is dropped,
    because the proposal moves that field out of the record and into a
    separate binding relation.

    Nothing else is normalised, and the exception matters. A census receipt
    sets `excerpt_sha256` to the snapshot id on purpose: its excerpt is the
    whole file inventory, so the inventory's digest is its content hash.
    Replacing the snapshot id wherever it appeared made two such receipts
    compare equal across two different inventories, which reported a
    whole-repository fact as reusable -- the one class of record this
    instrument exists to catch. A record that carries the snapshot id
    anywhere but the dropped field is stating something about the whole
    snapshot, and has to be recomputed when any file changes.
    """

    return "|".join(
        f"{field.name}={getattr(record, field.name)!s}"
        for field in dataclass_fields(record)
        if field.name != "snapshot_id"
    )


def _paths_of(record: Any, symbol_paths: dict[str, str]) -> set[str]:
    """Every file this record depends on, as the record states it.

    `path` for evidence and symbols, `source_path` for an edge, and the file
    named by any `file:` invalidation key for a claim. Read from the record
    rather than from a table of record types, so a new record class is handled
    by whichever of these it happens to carry.

    An edge depends on a second file, and missing it is the mistake an
    incremental implementation is most likely to make. A resolved edge carries
    the identifier of the symbol it lands on, that identifier follows the
    target file's bytes, and so editing a file renames every edge pointing
    into it from anywhere else in the repository. Editing this repository's
    Python reader moved 76 edges that no other rule here explains, every one of
    them declared in another file and resolving into the edited one. Keying an
    edge on its source file alone would have kept all 76 and served a
    resolution to a symbol that no longer exists under that name.
    """

    found = {
        str(getattr(record, "path", "") or ""),
        str(getattr(record, "source_path", "") or ""),
        symbol_paths.get(str(getattr(record, "target_symbol_id", "") or ""), ""),
    }
    for key in getattr(record, "invalidation_keys", ()) or ():
        if str(key).startswith("file:"):
            found.add(str(key).split(":", 1)[1])
    return {item for item in found if item}


def _producer(record: Any) -> str:
    return str(getattr(record, "analyzer", None) or getattr(record, "produced_by", "?"))


def examine(root: Path, edit: str | None) -> dict[str, Any]:
    """One repository, analyzed twice, with one line added in between."""

    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary) / root.name
        # `symlinks=False` copies what a link points at; the scanner refuses to
        # follow one out of the root, and a copy must not smuggle one in.
        shutil.copytree(root, work, symlinks=False, ignore_dangling_symlinks=True)

        before_snapshot = scan_repository(work)
        if not before_snapshot.files:
            return {}
        before = analyze_snapshot(before_snapshot)

        target = _choose_edit(work, before_snapshot, edit)
        if target is None:
            return {}
        target.write_text(
            target.read_text(encoding="utf-8", errors="strict") + ADDED_LINE, encoding="utf-8"
        )

        after_snapshot = scan_repository(work)
        after = analyze_snapshot(after_snapshot)

        was = {item.path: item.sha256 for item in before_snapshot.files}
        changed = {item.path for item in after_snapshot.files if was.get(item.path) != item.sha256}

        # Where each symbol lives, so an edge's dependency on the file it
        # resolves into can be read from the edge.
        symbol_paths = {item.symbol_id: item.path for item in after.symbols}

        measured: dict[str, Any] = {
            "repository": root.name,
            "files": len(after_snapshot.files),
            "edited": str(target.relative_to(work)).replace("\\", "/"),
            "changed_files": len(changed),
            "classes": {},
        }
        for name in RECORD_CLASSES:
            previous = Counter(canonical(item) for item in records_of(before, name))
            reusable = touched = unexplained = 0
            producers: Counter[str] = Counter()
            samples: list[str] = []
            records = records_of(after, name)
            for record in records:
                key = canonical(record)
                if previous.get(key, 0) > 0:
                    previous[key] -= 1
                    reusable += 1
                elif _paths_of(record, symbol_paths) & changed:
                    touched += 1
                else:
                    unexplained += 1
                    producers[_producer(record)] += 1
                    if len(samples) < 12:
                        samples.append(key)
            measured["classes"][name] = {
                "total": len(records),
                "reusable": reusable,
                "from_edited_file": touched,
                "unexplained": unexplained,
                "unexplained_by_producer": dict(producers.most_common()),
                "unexplained_samples": samples,
            }
        return measured


def records_of(result: Any, name: str) -> list[Any]:
    return list(getattr(result, name))


def _choose_edit(work: Path, snapshot: Any, edit: str | None) -> Path | None:
    """The file to change: the one named, or the largest source file there is.

    Largest rather than first, because an edit to a file nothing reads measures
    less than an edit to one the repository is built around, and the question
    is what an edit costs at its worst.
    """

    if edit is not None:
        candidate = work / edit
        return candidate if candidate.is_file() else None
    source = [item for item in snapshot.files if item.role == "source" and item.line_count > 0]
    if not source:
        return None
    chosen = max(source, key=lambda item: (item.size_bytes, item.path))
    return work / str(chosen.path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--edit", help="Repository-relative file to add a line to.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--show", type=int, default=6, help="Unexplained records to print.")
    parser.add_argument("repositories", type=Path, nargs="+")
    arguments = parser.parse_args()

    measured = []
    for repository in arguments.repositories:
        root = repository.expanduser().resolve(strict=True)
        found = examine(root, arguments.edit)
        if found:
            measured.append(found)

    if arguments.json:
        print(json.dumps({"runs": measured}, indent=2))
        return 0

    for run in measured:
        print(f"\n## {run['repository']}: {run['files']:,} files\n")
        print(f"  edited {run['edited']} ({run['changed_files']} file's bytes changed)\n")
        for name in RECORD_CLASSES:
            found = run["classes"][name]
            total = max(1, found["total"])
            print(
                f"  {name:9s} {found['total']:8,}   "
                f"reusable {found['reusable']:8,} ({found['reusable'] / total:5.1%})   "
                f"edited file {found['from_edited_file']:6,}   "
                f"unexplained {found['unexplained']:5,}"
            )
        for name in RECORD_CLASSES:
            found = run["classes"][name]
            if not found["unexplained"]:
                continue
            print(f"\n  {name} that a cache could not have kept safely:")
            for producer, count in found["unexplained_by_producer"].items():
                print(f"    {count:6,}  {producer}")
            for sample in found["unexplained_samples"][: arguments.show]:
                print(f"      ! {sample[:160]}")
    print(
        "\nReusable is a ceiling while identifiers still hash the snapshot id, and\n"
        "the reuse actually achieved once they do not. Unexplained records name no\n"
        "file that changed and match nothing from the previous run: they are\n"
        "whole-repository facts, and an incremental run has to recompute them.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
