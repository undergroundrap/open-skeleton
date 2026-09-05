# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""How many imports could resolve, and how many of those do?

The gap census reports that 8% of 48,016 import references resolve to a symbol
and stops there, which reads like a nine-in-ten failure and is not one. Most
of those references name `os`, `React` or `serde` -- something outside the
repository, which no amount of work inside it will ever resolve. Reporting a
rate against a denominator that includes them measures the ratio of borrowed
code to owned code, which is a fact about a project's dependencies rather than
about this engine.

So this separates the two before rating anything:

- **outside**: the reference names something the snapshot does not contain.
  Correctly unresolved, and counted so the rate has an honest denominator.
- **inside, resolved**: the engine found the symbol.
- **inside, unresolved**: the reference names a file this repository holds and
  the engine did not connect them. This is the number worth working on, and it
  is the only one this instrument treats as a gap.

Whether a reference is "inside" is decided by the snapshot's own file list
rather than by a list of standard-library names, which would be one list per
language and stale in all of them. A reference is inside when some file in the
repository could plausibly be what it names -- by path, by module path, or by
the last segment of either.

Relative references are translated with the resolver's own function rather
than a copy of it, because a copy goes stale: when the resolver learned that
Python spells `./x` as `.x`, a private copy here called every such reference
outside the repository and the measured rate fell while the engine improved.

That test is exact about the file and approximate about the intent, and a
repository that vendors a standard library defeats it: `mypy` ships typeshed,
so `from typing import Any` in its own source names a `typing.pyi` that really
is in the snapshot. On the first run of this instrument `mypy` supplied 13,713
of 18,709 missed references and `pip`, which vendors its dependencies, another
1,698. So the median repository is reported beside the pool and the largest
contributors are named. A pooled ratio nobody can see the skew in is the thing
to avoid; the skew itself is a fact worth printing.

    python benchmarks/generalization/run_resolution_ceiling.py --root .venv/Lib/site-packages
    python benchmarks/generalization/run_resolution_ceiling.py --repo one --repo two

Exit status is zero. This measures a ceiling; it is not a gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.resolution import _as_path
from open_skeleton.scanner import scan_repository

SKIP_SUFFIXES = (".dist-info", ".egg-info", ".egg")
# A reference's last segment, stripped of the punctuation each language uses to
# join them. One rule for six languages rather than six rules.
SEPARATORS = ("::", "/", ".")


def _tail(reference: str) -> str:
    """The last segment of a reference, however its language joins segments."""

    text = reference.strip().strip("\"'")
    for separator in SEPARATORS:
        text = text.rsplit(separator, 1)[-1]
    return text


PACKAGE_FILES = {"__init__", "mod", "index"}


def _repository_names(paths: list[str]) -> tuple[set[str], set[str]]:
    """What this repository could be named by: module paths, and sibling names.

    The first set answers a reference that spells out where it points, with
    the extension dropped and package files standing for their directory. The
    second holds `directory/stem` rather than the bare stem, so a sibling can
    only be matched from the directory it sits in -- `pydantic` bundles
    `v1/typing.py`, and a bare stem set called every `import typing` in the
    package an inside reference.
    """

    modules: set[str] = set()
    siblings: set[str] = set()
    for path in paths:
        pure = PurePosixPath(path)
        stem = path.rsplit(".", 1)[0]
        modules.add(path)
        modules.add(stem)
        parent = str(pure.parent) if str(pure.parent) != "." else ""
        siblings.add(f"{parent}/{pure.stem}" if parent else pure.stem)
        if pure.stem in PACKAGE_FILES and parent:
            modules.add(parent)
            grandparent = str(pure.parent.parent) if str(pure.parent.parent) != "." else ""
            siblings.add(f"{grandparent}/{pure.parent.name}" if grandparent else pure.parent.name)
    return modules, siblings


def _names_something_here(
    reference: str, source: str, modules: set[str], siblings: set[str]
) -> bool:
    """Whether this reference could be a file the snapshot holds.

    A reference carrying a separator spells out where it points, so it is
    resolved to a path and looked up. A single-segment reference names a
    sibling, and is looked up only in the directory of the file that wrote it:
    `import typing` inside `pydantic/v2` is the standard library even though
    `pydantic/v1/typing.py` exists.
    """

    text = reference.strip().strip("\"'")
    if not text:
        return False
    directory = str(PurePosixPath(source).parent)
    directory = "" if directory == "." else directory

    # Python's `.x` is `./x`, and the resolver owns that translation. Keeping
    # a second copy here made every Python relative import read as outside the
    # moment the resolver learned to connect them.
    if text.startswith(".") and not text.startswith(("./", "../")):
        text = _as_path(text)

    if text.startswith(("./", "../")):
        candidate = PurePosixPath(directory or ".").joinpath(text)
        resolved: list[str] = []
        for part in candidate.parts:
            if part == "..":
                if resolved:
                    resolved.pop()
            elif part not in {".", ""}:
                resolved.append(part)
        return "/".join(resolved) in modules

    if any(separator in text for separator in ("::", "/", ".")):
        flat = text.replace("::", "/").replace(".", "/")
        return flat in modules or f"{directory}/{flat}" in modules

    return (f"{directory}/{text}" if directory else text) in siblings


def examine(repository: Path) -> dict[str, object]:
    """Counts for one repository, and the references it could not connect."""

    snapshot = scan_repository(repository)
    if not snapshot.files:
        return {}
    result = analyze_snapshot(snapshot)
    paths = [item.path for item in snapshot.files]
    modules, siblings = _repository_names(paths)

    outside = inside_resolved = inside_unresolved = 0
    unconnected: Counter[str] = Counter()
    for edge in result.edges:
        if edge.relationship != "imports":
            continue
        reference = edge.target_ref
        is_inside = _names_something_here(reference, edge.source_path, modules, siblings)
        if not is_inside:
            outside += 1
        elif edge.target_symbol_id:
            inside_resolved += 1
        else:
            inside_unresolved += 1
            unconnected[_tail(reference)] += 1

    return {
        "outside": outside,
        "inside_resolved": inside_resolved,
        "inside_unresolved": inside_unresolved,
        "unconnected": unconnected,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Directory whose children are repositories.")
    parser.add_argument("--repo", action="append", type=Path, default=[])
    parser.add_argument("--show", type=int, default=15)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()

    targets = list(arguments.repo)
    if arguments.root is not None:
        targets += sorted(
            item
            for item in arguments.root.iterdir()
            if item.is_dir()
            and item.name != "__pycache__"
            and not item.name.endswith(SKIP_SUFFIXES)
        )
    if not targets:
        targets = [Path.cwd()]

    outside = inside_resolved = inside_unresolved = 0
    unconnected: Counter[str] = Counter()
    examined = 0
    # Per repository, so one unusual tree cannot set the headline for the rest.
    rates: list[float] = []
    contributors: list[tuple[int, str]] = []
    for target in targets:
        try:
            found = examine(target)
        except Exception as error:  # noqa: BLE001 - one bad repository is a row, not a stop
            print(f"  {target.name}: {error.__class__.__name__}: {error}")
            continue
        if not found:
            continue
        examined += 1
        outside += int(found["outside"])
        inside_resolved += int(found["inside_resolved"])
        inside_unresolved += int(found["inside_unresolved"])
        unconnected.update(found["unconnected"])  # type: ignore[arg-type]
        here = int(found["inside_resolved"]) + int(found["inside_unresolved"])
        if here:
            rates.append(int(found["inside_resolved"]) / here)
            contributors.append((int(found["inside_unresolved"]), target.name))

    inside = inside_resolved + inside_unresolved
    total = inside + outside
    if arguments.json:
        print(
            json.dumps(
                {
                    "repositories": examined,
                    "outside": outside,
                    "inside_resolved": inside_resolved,
                    "inside_unresolved": inside_unresolved,
                },
                indent=2,
            )
        )
        return 0

    print(f"\n## Repositories examined: {examined}\n")
    print(f"  import references          {total:,}")
    print(f"  name something outside     {outside:,} ({outside / max(1, total):.0%})")
    print(f"  name something inside      {inside:,} ({inside / max(1, total):.0%})")
    print()
    print(
        f"  of those inside, resolved  {inside_resolved:,} ({inside_resolved / max(1, inside):.0%})"
    )
    print(
        f"  of those inside, missed    {inside_unresolved:,} ({inside_unresolved / max(1, inside):.0%})"
    )
    if rates:
        ordered = sorted(rates)
        median = ordered[len(ordered) // 2]
        print(f"\n  median repository resolves {median:.0%} of its inside references")
    print(
        "\nA reference naming something outside the repository is correctly "
        "unresolved and\nis not a gap. The rate worth acting on is the second "
        "pair: a file this repository\nholds, named by an import this engine did "
        "not connect to it.\n"
    )

    print("### Repositories supplying the most missed references\n")
    print("Read this before the pooled rate. A repository that vendors a standard")
    print("library names it from inside and defeats the test above: `mypy` ships")
    print("typeshed, `pip` ships its dependencies, and neither is a defect in the")
    print("resolver.\n")
    contributors.sort(reverse=True)
    for count, name in contributors[: arguments.show]:
        share = count / max(1, inside_unresolved)
        print(f"  {count:7,} ({share:4.0%})  {name}")
    print()

    print("### Names imported from inside and left unconnected\n")
    if not unconnected:
        print("  none")
    for name, count in unconnected.most_common(arguments.show):
        print(f"  {count:6,}  {name}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
