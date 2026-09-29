# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""When one file changes, what else stops being true?

Reusing an unchanged file's facts is only correct for facts that depend on
that file and nothing else. A fact that quietly depends on the rest of the
repository, cached and served under a snapshot where the rest has changed, is
a wrong answer recorded as a fact -- which this engine treats as worse than a
slow one. So the dependency has to be measured before anything is cached.

Three changes are made, one at a time, and after each one every reader is
asked what it now says about the files that did *not* change:

- **edit**: one file's bytes change and the file list does not.
- **add**: a new file appears.
- **remove**: an existing file disappears.

These are separated because they are not equally safe, and the difference is
the whole design of a cache. An edit is local. Adding or removing a file is
not necessarily local at all, because a reader may read the file list itself:
Python's qualified names are built from package roots, so whether
`src/open_skeleton/__init__.py` is in the snapshot decides whether a receipt
names `open_skeleton.analyzers.project_metadata._declared_license` or
`analyzers.project_metadata._declared_license`. Every Python receipt in the
repository is renamed by that, having read no file that changed.

The first version of this instrument deleted every second file, found 4,306
Python receipts renamed, and was about to be read as "the Python reader is not
per-file". It is -- the perturbation had deleted the package markers. A
measurement has to perturb the way a repository actually changes, or it
reports the shape of its own experiment.

A reader the corpus exercises with no files is reported as `none` rather than
as passing. A check that reads zero everywhere looks exactly like a clean bill
of health, and this repository has shipped one before.

    python benchmarks/scaling/run_reader_locality.py -- <repository> [...]

The repository is copied to a temporary directory before being changed; the
path given is never written to. Exit status is zero: this enumerates, it does
not gate.
"""

from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import shutil
import sys
import tempfile
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import open_skeleton
from open_skeleton import models

PINNED_CLOCK = "2026-01-01T00:00:00.000+00:00"
RECORD_CLASSES = ("evidence", "symbols", "edges", "claims")
ADDED_LINE = "\n# open-skeleton reader-locality measurement\n"
ADDED_FILE = "open_skeleton_locality_probe.py"


def _pin_clock() -> None:
    def pinned() -> str:
        return PINNED_CLOCK

    models.utc_now = pinned
    for info in pkgutil.walk_packages(open_skeleton.__path__, "open_skeleton."):
        module = importlib.import_module(info.name)
        if hasattr(module, "utc_now"):
            module.utc_now = pinned  # type: ignore[attr-defined]


_pin_clock()

from open_skeleton.analysis import build_analyzers  # noqa: E402
from open_skeleton.scanner import scan_repository  # noqa: E402


def canonical(record: Any) -> str:
    """A record's whole content, less the snapshot it was produced under."""

    return "|".join(
        f"{field.name}={getattr(record, field.name)!s}"
        for field in dataclass_fields(record)
        if field.name != "snapshot_id"
    )


def identity(record: Any) -> str:
    """What a cache would key this record on.

    A claim is keyed on its content instead of its identifier, because a claim
    id hashes the snapshot on purpose -- that is what lets two files stating
    the same thing merge into one claim carrying both receipts. Comparing
    claim ids across two snapshots therefore reports every claim as renamed,
    every time, which reads like a locality failure and says nothing. What is
    worth asking of a claim is whether the reader still states it.
    """

    if type(record).__name__ == "ClaimRecord":
        return "|".join(
            f"{field.name}={getattr(record, field.name)!s}"
            for field in dataclass_fields(record)
            if field.name not in {"snapshot_id", "claim_id"}
        )
    for field in dataclass_fields(record):
        if field.name.endswith("_id") and field.name != "snapshot_id":
            return str(getattr(record, field.name))
    return canonical(record)


def _content(record: Any) -> str:
    """What the record says, compared against what a cache would have kept."""

    return identity(record) if type(record).__name__ == "ClaimRecord" else canonical(record)


def _paths_of(record: Any) -> set[str]:
    found = {
        str(getattr(record, "path", "") or ""),
        str(getattr(record, "source_path", "") or ""),
    }
    for key in getattr(record, "invalidation_keys", ()) or ():
        if str(key).startswith("file:"):
            found.add(str(key).split(":", 1)[1])
    return {item for item in found if item}


def _read_all(root: Path) -> dict[str, Any]:
    snapshot = scan_repository(root)
    return {
        "snapshot": snapshot,
        "by_reader": {
            str(getattr(reader, "name", reader.__class__.__name__)): reader.analyze(snapshot)
            for reader in build_analyzers()
        },
    }


def _compare(before: Any, after: Any, untouched: set[str]) -> dict[str, Any]:
    """What a reader now says about files that did not change."""

    classes: dict[str, Any] = {}
    for collection in RECORD_CLASSES:

        def attributable(result: Any, collection: str = collection) -> list[Any]:
            found = []
            for record in getattr(result, collection):
                where = _paths_of(record)
                if where and where <= untouched:
                    found.append(record)
            return found

        expected = {identity(item): _content(item) for item in attributable(before)}
        produced = {identity(item): _content(item) for item in attributable(after)}
        if not expected and not produced:
            classes[collection] = {"exercised": False}
            continue
        vanished = sorted(set(expected) - set(produced))
        appeared = sorted(set(produced) - set(expected))
        moved = sorted(
            key for key in set(expected) & set(produced) if expected[key] != produced[key]
        )
        classes[collection] = {
            "exercised": True,
            "total": len(expected),
            "vanished": len(vanished),
            "appeared": len(appeared),
            "same_identity_different_content": len(moved),
            "local": not (vanished or appeared or moved),
            "samples": [expected[key][:190] for key in (moved or vanished)[:2]],
        }
    return classes


def _largest_source(snapshot: Any) -> str | None:
    source = [item for item in snapshot.files if item.role == "source" and item.line_count > 0]
    if not source:
        return None
    return str(max(source, key=lambda item: (item.size_bytes, item.path)).path)


def examine(root: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary) / root.name
        # `.git` is skipped because its objects are read-only on Windows and
        # undoing a change by deleting the tree then fails; nothing here reads
        # it in any case.
        shutil.copytree(
            root,
            work,
            symlinks=False,
            ignore_dangling_symlinks=True,
            ignore=shutil.ignore_patterns(".git"),
        )
        base = _read_all(work)
        target = _largest_source(base["snapshot"])
        if target is None:
            return {}
        every_path = {item.path for item in base["snapshot"].files}

        found: dict[str, Any] = {
            "repository": root.name,
            "files": len(base["snapshot"].files),
            "target": target,
            "changes": {},
        }

        # Undo exactly what was done rather than rebuilding the tree, so every
        # change starts from the same bytes the base run measured.
        original = (work / target).read_bytes()

        for change in ("edit", "add", "remove"):
            if change == "edit":
                path = work / target
                path.write_text(path.read_text(encoding="utf-8") + ADDED_LINE, encoding="utf-8")
                untouched = every_path - {target}
            elif change == "add":
                (work / ADDED_FILE).write_text("VALUE = 1\n", encoding="utf-8")
                untouched = every_path
            else:
                (work / target).unlink()
                untouched = every_path - {target}

            after = _read_all(work)
            found["changes"][change] = {
                name: _compare(base["by_reader"][name], after["by_reader"][name], untouched)
                for name in base["by_reader"]
            }

            if change == "add":
                (work / ADDED_FILE).unlink()
            else:
                (work / target).write_bytes(original)
        return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("repositories", type=Path, nargs="+")
    arguments = parser.parse_args()

    measured = []
    for repository in arguments.repositories:
        found = examine(repository.expanduser().resolve(strict=True))
        if found:
            measured.append(found)

    if arguments.json:
        print(json.dumps({"runs": measured}, indent=2))
        return 0

    for run in measured:
        print(f"\n## {run['repository']}: {run['files']:,} files, changing {run['target']}\n")
        for change, readers in run["changes"].items():
            print(f"  --- {change} ---")
            for name, classes in readers.items():
                cells: list[str] = []
                detail: list[str] = []
                for collection in RECORD_CLASSES:
                    found = classes[collection]
                    label = collection[:4]
                    if not found["exercised"]:
                        cells.append(f"{label}:none")
                    elif found["local"]:
                        cells.append(f"{label}:local({found['total']:,})")
                    else:
                        moved = found["same_identity_different_content"]
                        cells.append(
                            f"{label}:-{found['vanished']}/+{found['appeared']}"
                            + (f"/~{moved}" if moved else "")
                        )
                        detail.extend(f"        {collection}: {s}" for s in found["samples"])
                print(f"    {name:26s} {'  '.join(cells)}")
                for line in detail:
                    print(line)
            print()
    print(
        "local    the reader said exactly the same thing about every file that did not\n"
        "         change: safe to reuse for those files under this kind of change\n"
        "-n/+n    records that came or went although no file they name was touched\n"
        "~n       SAME identifier, different content: a cache would hit and be wrong\n"
        "none     this corpus exercised the reader with nothing; no verdict was reached\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
