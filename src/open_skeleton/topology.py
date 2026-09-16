# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Who owns a module, and who reaches past the door.

Work package 2 asks for ownership and shared-facade topology, and sets one
condition on it: every statement carries a path of edge and evidence IDs, and
an ambiguous target stays unresolved. So nothing here is a ratio, a threshold,
or a ranking. Two facts are reported, both exact, both true by counting:

- **a module with exactly one importer**. One caller to consider when it
  changes, and the edge that says so is the whole evidence.
- **a package facade crossed or used**. A directory holding `__init__.py`,
  `mod.rs` or `index.ts` has a door, which is a fact about the layout rather
  than a judgement about it. An import from outside the package either names
  that file or names something behind it, and counting the two separately says
  whether the package's public surface is what its facade exports.

Both read only resolved edges. An import whose target this engine could not
identify says nothing about ownership, and counting it would turn the shape of
the resolver into a claim about the repository.

Nothing here judges. "185 imports from outside `open_skeleton` name a module
inside it and none name the package" is a fact; whether a curated `__init__`
was intended is not something a reader of source can know, and the claim says
what was counted rather than what it implies.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from open_skeleton.models import EdgeRecord, FileRecord, SymbolRecord

# A file whose stem is one of these stands for the directory holding it: it is
# the name an importer outside that directory reaches for. `lib` and `main` are
# Rust crate roots, which play the same part for a crate that `__init__` plays
# for a package.
FACADE_STEMS = frozenset({"__init__", "mod", "index", "lib", "main"})
# Names listed before a claim starts counting instead. Long enough to be useful
# to a reader, short enough that the sentence stays a sentence.
MAX_NAMED = 8


@dataclass(frozen=True, slots=True)
class PackageDoor:
    """How a package is entered from outside it, counted both ways."""

    package: str
    facade: str
    through: int = 0
    around: int = 0
    through_evidence: tuple[str, ...] = ()
    around_evidence: tuple[str, ...] = ()


@dataclass(slots=True)
class Topology:
    """What resolved import edges say about who owns what."""

    sole_owned: dict[str, str] = field(default_factory=dict)
    sole_evidence: dict[str, str] = field(default_factory=dict)
    doors: tuple[PackageDoor, ...] = ()
    resolved_edges: int = 0


def _stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _directory(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def facades(files: Sequence[FileRecord]) -> dict[str, str]:
    """Each package directory that has a door, mapped to the file that is it.

    A directory with two facade files -- a Rust crate holding both `lib.rs` and
    `main.rs` -- has two doors and no single one, so it is left out rather than
    resolved by preferring one. That is the same refusal the import resolver
    makes for an ambiguous target, and for the same reason.
    """

    found: dict[str, list[str]] = defaultdict(list)
    for item in files:
        directory = _directory(item.path)
        if directory and _stem(item.path) in FACADE_STEMS:
            found[directory].append(item.path)
    return {directory: paths[0] for directory, paths in found.items() if len(paths) == 1}


def describe(
    files: Sequence[FileRecord],
    symbols: Sequence[SymbolRecord],
    edges: Sequence[EdgeRecord],
) -> Topology:
    """Ownership facts from resolved import edges, and nothing else.

    A self-import is dropped -- a package file importing its own submodule
    names the directory it lives in -- and so is an edge whose target this
    engine could not identify.
    """

    path_by_symbol = {item.symbol_id: item.path for item in symbols}
    doors = facades(files)

    importers: defaultdict[str, dict[str, str]] = defaultdict(dict)
    through: defaultdict[str, list[str]] = defaultdict(list)
    around: defaultdict[str, list[str]] = defaultdict(list)
    resolved = 0

    for edge in edges:
        if edge.relationship != "imports" or not edge.target_symbol_id:
            continue
        target = path_by_symbol.get(edge.target_symbol_id)
        if not target or target == edge.source_path:
            continue
        resolved += 1
        importers[target].setdefault(edge.source_path, edge.evidence_id or "")

        package = _directory(target)
        facade = doors.get(package)
        if facade is None:
            continue
        if edge.source_path == facade or edge.source_path.startswith(f"{package}/"):
            # Inside the package, where reaching a sibling directly is how a
            # package is written rather than a way around anything.
            continue
        if target == facade:
            through[package].append(edge.evidence_id or "")
        else:
            around[package].append(edge.evidence_id or "")

    sole_owned: dict[str, str] = {}
    sole_evidence: dict[str, str] = {}
    for target, sources in importers.items():
        if len(sources) != 1:
            continue
        owner, evidence = next(iter(sources.items()))
        sole_owned[target] = owner
        sole_evidence[target] = evidence

    described = tuple(
        PackageDoor(
            package=package,
            facade=doors[package],
            through=len(through.get(package, ())),
            around=len(around.get(package, ())),
            through_evidence=tuple(item for item in through.get(package, ()) if item),
            around_evidence=tuple(item for item in around.get(package, ()) if item),
        )
        for package in sorted(set(through) | set(around))
    )
    return Topology(
        sole_owned=sole_owned,
        sole_evidence=sole_evidence,
        doors=described,
        resolved_edges=resolved,
    )
