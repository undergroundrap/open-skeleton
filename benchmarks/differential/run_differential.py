# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Compare this engine's TypeScript reading against a real parser's.

Every other check here needs someone to imagine the input first. Fixtures are
written by a person who already knows the answer, and a corpus sweep only
covers shapes some repository happened to contain. Both miss the same thing:
a form nobody thought of. `export { type Foo, Bar }` published an export named
`type` for as long as it did because no test and no scanned repository used
the inline type modifier.

A differential test needs no such imagination. Feed both readers the same file
and any disagreement is a lead, whoever turns out to be right.

The reference is esbuild, chosen because it reports what the module system
actually binds. That is not quite the question this engine asks, and the
difference is the useful part:

* **A name esbuild reports and this engine does not is a defect.** The module
  exports it at run time and the specification omitted it.
* **A name this engine reports and esbuild does not needs reading.** It is
  usually a type export, which esbuild erases because types do not exist at
  run time and which belongs in a specification because removing one breaks
  every TypeScript consumer. It can also be a fabrication.

That asymmetry already paid for itself: `export default class Engine {}` was
reported as exporting `Engine`, while esbuild reports `default`. An importer
writes `import Anything from "./x"`, so renaming the class breaks nobody, and
the claim that renaming it is a breaking change was simply false.

esbuild is a development dependency and is never required to analyze a
repository. Without it this exits zero and says so, because a check that
cannot run is not a check that failed.

    npm install esbuild
    python benchmarks/differential/run_differential.py --root some/typescript/project
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.analyzers.typescript_lexical import (
    _exported_names,
    _tokens,
)

SOURCE_SUFFIXES = {".ts", ".tsx", ".mts", ".js", ".jsx", ".mjs"}
# Directories whose contents are someone else's build output or dependency.
SKIP_DIRECTORIES = {"node_modules", "dist", "build", ".git", "coverage", "out"}
MAX_FILES = 400


@dataclass
class Disagreement:
    path: str
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()


@dataclass
class Report:
    compared: int = 0
    skipped: int = 0
    defects: list[Disagreement] = field(default_factory=list)
    to_read: list[Disagreement] = field(default_factory=list)


def _esbuild(explicit: Path | None = None) -> str | None:
    if explicit is not None:
        return str(explicit) if explicit.exists() else None
    direct = shutil.which("esbuild")
    if direct:
        return direct
    for candidate in (
        Path("node_modules/.bin/esbuild.cmd"),
        Path("node_modules/.bin/esbuild"),
    ):
        if candidate.exists():
            return str(candidate.resolve())
    return None


def _reference_exports(binary: str, source: Path, work: Path) -> set[str] | None:
    """Names esbuild reports the module binding, or None if it could not parse."""

    meta = work / "meta.json"
    result = subprocess.run(  # noqa: S603 — fixed binary, path argument
        [
            binary,
            str(source),
            "--format=esm",
            f"--metafile={meta}",
            f"--outfile={work / 'out.js'}",
            "--log-level=silent",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not meta.exists():
        return None
    payload = json.loads(meta.read_text(encoding="utf-8"))
    names: set[str] = set()
    for output in payload.get("outputs", {}).values():
        names.update(output.get("exports", []))
    return names


def _candidates(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(found) >= MAX_FILES:
            break
        if path.suffix not in SOURCE_SUFFIXES or not path.is_file():
            continue
        if any(part in SKIP_DIRECTORIES for part in path.parts):
            continue
        if path.name.endswith((".d.ts", ".test.ts", ".spec.ts")):
            continue
        found.append(path)
    return found


def compare(root: Path, binary: str) -> Report:
    report = Report()
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        for source in _candidates(root):
            reference = _reference_exports(binary, source, work)
            if reference is None:
                report.skipped += 1
                continue
            report.compared += 1
            ours = set(
                _exported_names(_tokens(source.read_text(encoding="utf-8", errors="replace")))
            )
            missing = reference - ours
            extra = ours - reference
            relative = str(source.relative_to(root)).replace("\\", "/")
            if missing:
                report.defects.append(Disagreement(relative, missing=tuple(sorted(missing))))
            if extra:
                report.to_read.append(Disagreement(relative, extra=tuple(sorted(extra))))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="TypeScript project to compare.")
    parser.add_argument(
        "--strict", action="store_true", help="Exit non-zero if any export is missing."
    )
    parser.add_argument(
        "--esbuild",
        type=Path,
        default=None,
        help="Path to the esbuild binary, when it is not on PATH or in ./node_modules.",
    )
    arguments = parser.parse_args()

    binary = _esbuild(arguments.esbuild)
    if binary is None:
        print("esbuild not found; skipping. Install it with `npm install esbuild`.")
        return 0

    report = compare(arguments.root.expanduser().resolve(strict=True), binary)
    print(f"files compared: {report.compared:,}  (esbuild could not parse {report.skipped:,})")
    print(f"  exports we miss:      {len(report.defects):,} file(s)")
    print(f"  names esbuild erases: {len(report.to_read):,} file(s)")

    if report.defects:
        print("\nDEFECTS — the module exports these and the specification omits them:")
        for item in report.defects[:20]:
            print(f"  {item.path}: {', '.join(item.missing)}")
    if report.to_read:
        print("\nTO READ — usually type exports, which belong in a specification:")
        for item in report.to_read[:10]:
            print(f"  {item.path}: {', '.join(item.extra)}")

    return 1 if arguments.strict and report.defects else 0


if __name__ == "__main__":
    raise SystemExit(main())
