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

# Readers whose call edges say whether a call had a receiver. Only those may
# have their calls bound, and this is a statement about the reader rather than
# about the language.
#
# Given `text(1)` and `w.text()`, the Python reader records `text` and
# `w.text`; the Rust and TypeScript readers both record `text` twice. A bare
# name from those readers is a free call and a method call at once, so binding
# it to the free function of that name is a coin toss written down as an edge
# -- and an agent walking this graph would have no way to know. Measured on
# one Rust crate, that mistake produced 254 resolved calls to `new`, the name
# every Rust constructor has.
#
# This is the whole barrier: a reader that records its receivers joins the
# list and its calls resolve with no further work here.
CALL_RECEIVER_ANALYZERS = ("python-ast/",)


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


def _as_path(reference: str) -> str:
    """A Python relative import written the way `./x` is written.

    One leading dot means this package, and each dot after it means one level
    up: `.a.b` is `./a/b`, `..a` is `../a`, `...a` is `../../a`. The remaining
    dots separate names and become directory separators, which is what they
    already mean.
    """

    level = len(reference) - len(reference.lstrip("."))
    body = reference[level:].replace(".", "/")
    prefix = "./" if level <= 1 else "../" * (level - 1)
    return prefix + body if body else prefix.rstrip("/")


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

    Python's relative import is the same idea in different punctuation: `.x`
    is `./x` and `..x.y` is `../x/y`, one leading dot per level. It is handled
    with the other relative form rather than beside it, because they are one
    rule about position and not two rules about syntax.
    """

    reference = reference.strip()
    if not reference:
        return []
    if reference.startswith(("./", "../")):
        return _relative_candidates(reference, source_path)
    if reference.startswith("."):
        return _relative_candidates(_as_path(reference), source_path)
    if reference.startswith("crate::"):
        return [reference[len("crate::") :]]
    if reference.startswith(("super::", "self::")) or reference in {"super", "self", "crate"}:
        return []
    return [reference]


def _bound_names(edges: Sequence[EdgeRecord]) -> dict[str, dict[str, str]]:
    """Per file, the local name each resolved import binds to a symbol.

    `from open_skeleton.providers import ClaudeCliProvider` binds the name
    `ClaudeCliProvider` in that file and nowhere else. This is the whole basis
    for resolving a call: the file said what the name means.
    """

    bound: dict[str, dict[str, str]] = {}
    for edge in edges:
        if edge.relationship != "imports" or not edge.target_symbol_id:
            continue
        local = edge.target_ref.replace("::", ".").rstrip(".").split(".")[-1]
        if local:
            bound.setdefault(edge.source_path, {})[local] = edge.target_symbol_id
    return bound


def resolve_call_targets(
    symbols: Sequence[SymbolRecord],
    edges: Sequence[EdgeRecord],
) -> tuple[EdgeRecord, ...]:
    """Bind a call to a definition, where the file itself says what the name means.

    Two bindings, both written down in the source and neither inferred:

    1. the file imports the name, so the import says which symbol it is;
    2. the calling symbol's own module defines it.

    Everything else stays unresolved, and most calls should. Measured across
    three repositories, 53% to 83% of call targets name nothing the repository
    defines at all -- `len`, `append`, `clone`, `push_str`, `toBe` -- and
    another 17% are methods on `self`, whose definition needs a type this
    engine does not infer. The unambiguously bindable share is about one call
    in six, so a resolver reporting 15% is near its ceiling rather than far
    from a hundred.

    That ceiling is the useful number and it is easy to mistake for failure.
    An edge left empty because the call leaves the repository is a correct
    answer, not a gap.
    """

    by_name: dict[str, list[SymbolRecord]] = {}
    for symbol in symbols:
        by_name.setdefault(symbol.qualified_name, []).append(symbol)
    symbol_by_id = {item.symbol_id: item for item in symbols}
    bound = _bound_names(edges)

    resolved: list[EdgeRecord] = []
    for edge in edges:
        if edge.relationship != "calls" or edge.target_symbol_id:
            resolved.append(edge)
            continue
        if not edge.analyzer.startswith(CALL_RECEIVER_ANALYZERS):
            resolved.append(edge)
            continue
        reference = edge.target_ref.strip()
        # `self.render` and `this.render` name a method on a value whose type
        # is not written at the call site. Picking the one class that happens
        # to define `render` would be a guess wearing a citation.
        if not reference or reference.startswith(("self.", "this.")):
            resolved.append(edge)
            continue

        target = _bound_call(reference, edge, bound, by_name, symbol_by_id)
        resolved.append(replace(edge, target_symbol_id=target.symbol_id) if target else edge)
    return tuple(resolved)


def _bound_call(
    reference: str,
    edge: EdgeRecord,
    bound: dict[str, dict[str, str]],
    by_name: dict[str, list[SymbolRecord]],
    symbol_by_id: dict[str, SymbolRecord],
) -> SymbolRecord | None:
    parts = reference.replace("::", ".").split(".")
    head, rest = parts[0], parts[1:]

    imported = bound.get(edge.source_path, {}).get(head)
    if imported and imported in symbol_by_id:
        anchor = symbol_by_id[imported]
        if not rest:
            return anchor
        # `providers.ClaudeCliProvider` where the file imported `providers`.
        # Only an exact hit counts: binding the call to the module because
        # the member could not be found would report a call that never
        # happened at a place that never made it.
        deeper = by_name.get(f"{anchor.qualified_name}.{'.'.join(rest)}", [])
        return deeper[0] if len(deeper) == 1 else None

    caller = symbol_by_id.get(edge.source_symbol_id or "")
    if caller is None:
        return None
    # Rust spells a qualified name `parser::parse_source` and Python spells it
    # `parser.parse_source`. Splitting on `.` alone left every Rust caller's
    # module equal to its own full name, so the same-module lookup could never
    # hit and this reader resolved 163 of 36,898 calls in a Rust repository.
    module, separator = _module_of(caller)
    same_module = by_name.get(f"{module}{separator}{reference}", [])
    return same_module[0] if len(same_module) == 1 else None


def _module_of(caller: SymbolRecord) -> tuple[str, str]:
    """The module a call written inside this symbol can see, and its separator.

    A module's own module is itself. Taking the parent instead let the lookup
    escape one level up and out of the file: a call to `new` inside
    `warmboot_game::graphics_ui` searched `warmboot_game::` and bound to a
    free function in `main.rs`. 254 of one repository's 262 resolved calls
    were that, all of them wrong, and `new` is the name every Rust
    constructor has -- which is exactly why a bare name may only ever be
    looked up in the scope that can actually see it.
    """

    qualified = caller.qualified_name
    separator = "::" if "::" in qualified else "."
    if caller.kind == "module":
        return qualified, separator
    if separator in qualified:
        return qualified.rsplit(separator, 1)[0], separator
    return qualified, separator


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


PYTHON_SUFFIXES = (".py", ".pyi")


def _package_root_modules(files: Sequence[FileRecord]) -> frozenset[str]:
    """Top-level module names that an absolute Python import cannot mean.

    Empty unless the snapshot root holds an `__init__.py`, which makes the
    root the inside of a package: a module beside it is `package.module` to
    everyone else and `.module` from within, never the bare name. So
    `pydantic`, which ships `warnings.py`, resolved 26 `import warnings` edges
    to its own file, and each was the standard library.

    A root without `__init__.py` is a project root. There `import mypkg.thing`
    really does name `mypkg/thing.py`, so nothing is withheld.
    """

    paths = {item.path for item in files}
    if "__init__.py" not in paths:
        return frozenset()
    names: set[str] = set()
    for path in paths:
        head, _, rest = path.partition("/")
        if not rest:
            stem, dot, suffix = head.rpartition(".")
            if dot and f".{suffix}" in PYTHON_SUFFIXES and stem != "__init__":
                names.add(stem)
        elif rest == "__init__.py":
            names.add(head)
    return frozenset(names)


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
    package_modules = _package_root_modules(files)
    resolved: list[EdgeRecord] = []
    for edge in edges:
        if edge.relationship != "imports" or edge.target_symbol_id:
            resolved.append(edge)
            continue
        if edge.source_path not in known:
            resolved.append(edge)
            continue
        # An absolute import from inside a package goes to `sys.path`, which
        # is the directory above this snapshot. Resolving it to a file at the
        # root is how `import warnings` became `pydantic/warnings.py`.
        if (
            package_modules
            and edge.source_path.endswith(PYTHON_SUFFIXES)
            and not edge.target_ref.startswith(".")
            and edge.target_ref.split(".", 1)[0] in package_modules
        ):
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
