# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Give an import edge the symbol it refers to, or leave it unresolved.

The graph held 20,481 edges for this repository and 2,857 resolved targets,
and every resolved one was a `contains` edge -- a file holding a symbol it
declares. Every `calls` and every `imports` edge carried a name and no target.
That is not a small gap in a topology: work package 2 asks for owners, shared
facades and fan-in, and its exit proof is a path of edge and evidence IDs. A
name with no target is not a path, so none of those statements could be made
at all.

Imports are where resolution is honest, because the reference is explicit.
`from open_skeleton.providers import ClaudeCliProvider` names a module and a
symbol; nothing is inferred from spelling or proximity. So this joins on the
qualified name and nothing else, and a reference matching two symbols or none
stays unresolved rather than being adjudicated.

What stopped the join from working outside Python was not the join. Python
writes an absolute dotted path, which already equals a symbol's qualified
name. Rust writes `crate::units::Watts` and `super::mm`, TypeScript writes
`./skillCards` -- references relative to something, which match nothing until
the something is applied. Measured across the corpus before this existed:
Python repositories would join 46% of their imports, Rust 3% to 21%, and
TypeScript zero. The rest are `pathlib`, `serde` and `next`: real references
to code this repository does not contain, and correctly not resolved to
anything in it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import PurePosixPath

from open_skeleton.models import EdgeRecord, FileRecord, SymbolRecord

# Suffixes a module reference omits. A TypeScript import names `./skillCards`
# and the file is `skillCards.ts`, `skillCards.tsx`, or a directory holding
# `index.ts`; which one is a fact about the repository, not a preference.
MODULE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".py", ".rs")
INDEX_STEMS = ("index", "mod", "__init__")


def _dotted(path: str) -> str:
    """The dotted module name a path would carry, without its extension."""

    pure = PurePosixPath(path)
    stem = pure.name
    for suffix in MODULE_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    parts = [*pure.parent.parts, stem] if str(pure.parent) not in {"", "."} else [stem]
    return ".".join(part for part in parts if part)


def _relative_candidates(reference: str, source_path: str) -> list[str]:
    """Module names a `./x` or `../x` reference could name, from this file."""

    base = PurePosixPath(source_path).parent
    target = base / reference
    # `PurePosixPath` keeps `..` segments, and a name containing one matches
    # no symbol. Resolving them by hand keeps this independent of any
    # filesystem, which matters because a snapshot is not the working tree.
    parts: list[str] = []
    for part in target.parts:
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    if not parts:
        return []
    stem = _dotted("/".join(parts))
    return [stem, *(f"{stem}.{name}" for name in INDEX_STEMS)]


def _candidates(reference: str, source_path: str) -> list[str]:
    """Every name this reference could be, most specific first.

    Deliberately small. Each entry is a spelling the language defines, not a
    guess at what an author meant: a relative path resolved against the file
    holding it, a crate-root path with its prefix removed, and the reference
    itself. `super::` is absent because a parent module cannot be identified
    from a file stem, and inventing one would resolve edges to the wrong
    symbol rather than leave them honestly unresolved.
    """

    reference = reference.strip()
    if not reference:
        return []
    if reference.startswith(("./", "../")):
        return _relative_candidates(reference, source_path)
    if reference.startswith("crate::"):
        return [reference[len("crate::") :]]
    if reference.startswith(("super::", "self::")) or reference in {"super", "self", "crate"}:
        return []
    return [reference]


def _crate_roots(files: Iterable[FileRecord]) -> tuple[str, ...]:
    """Directories holding a Cargo manifest, longest first."""

    roots = {
        str(PurePosixPath(item.path).parent)
        for item in files
        if PurePosixPath(item.path).name.casefold() == "cargo.toml"
    }
    return tuple(sorted(roots, key=len, reverse=True))


def _crate_of(path: str, roots: Sequence[str]) -> str | None:
    for root in roots:
        if root in {"", "."}:
            return root
        if path == root or path.startswith(f"{root}/"):
            return root
    return None


def _index(symbols: Iterable[SymbolRecord]) -> dict[str, list[SymbolRecord]]:
    found: dict[str, list[SymbolRecord]] = {}
    for symbol in symbols:
        found.setdefault(symbol.qualified_name, []).append(symbol)
    return found


def resolve_import_targets(
    files: Sequence[FileRecord],
    symbols: Sequence[SymbolRecord],
    edges: Sequence[EdgeRecord],
) -> tuple[EdgeRecord, ...]:
    """Fill in `target_symbol_id` for imports naming exactly one symbol here.

    Three rules, in order, and a reference failing all three keeps no target:

    1. the qualified name as written, which is what Python already emits;
    2. a relative path resolved against the file holding it, which is what
       TypeScript emits, including the `index` and `mod` forms a directory
       reference stands for;
    3. a crate-root path with `crate::` removed, which is what Rust emits.

    Ambiguity is never adjudicated. A workspace with two crates each holding a
    `units.rs` gives `units::Watts` twice, and picking one would be a coin
    toss recorded as a fact -- so the edge stays unresolved and says so by
    carrying no target.
    """

    by_name = _index(symbols)
    known = {item.path for item in files}
    roots = _crate_roots(files)
    resolved: list[EdgeRecord] = []
    for edge in edges:
        if edge.relationship != "imports" or edge.target_symbol_id:
            resolved.append(edge)
            continue
        if edge.source_path not in known:
            resolved.append(edge)
            continue
        # `crate::` means this crate. A workspace where two members both
        # define `units` would otherwise resolve one member's reference to
        # the other's symbol whenever only one of them declares it in a file
        # -- a wrong answer, not an ambiguous one, so no ambiguity check
        # would catch it. Nothing in the corpus has that shape today, which
        # is exactly why it is worth stating rather than waiting for.
        crate = (
            _crate_of(edge.source_path, roots) if edge.target_ref.startswith("crate::") else None
        )
        target: SymbolRecord | None = None
        for candidate in _candidates(edge.target_ref, edge.source_path):
            found = by_name.get(candidate, ())
            if crate is not None:
                found = [item for item in found if _crate_of(item.path, roots) == crate]
            if len(found) == 1:
                target = found[0]
                break
            if found:
                # Named more than once here. Stop rather than fall through to
                # a vaguer candidate that might match one thing by accident.
                break
        resolved.append(replace(edge, target_symbol_id=target.symbol_id) if target else edge)
    return tuple(resolved)
