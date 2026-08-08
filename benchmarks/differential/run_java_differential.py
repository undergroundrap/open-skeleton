# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Check the Java declaration reader against a real compiler.

A fixture suite proves a reader handles the shapes someone thought to write
down. It says nothing about the shapes nobody thought of, and those are where
lexical readers fail. So this compares what we extract against what
`javac -Xprint` prints, over as much real Java as is available.

`javac -Xprint` runs the front end and prints declarations without generating
code. It resolves nothing it does not need to, so it works on a checkout that
was never built -- which is what makes it usable as an oracle here.

Two limits decide how the result should be read.

The first is that a differential says the two readers disagree. It never says
which one is wrong. On the first full run against `java.base` this harness
reported six inventions, and all six were real member classes that the
*reference parser below* had lost: `javac -Xprint` echoes doc comments, and
the braces inside `{@code ...}` desynchronised a depth counter that assumed
every brace was structure. The reader under test was right every time.

The second is that with an incomplete classpath `javac -Xprint` silently drops
every annotation from its output, reports the errors only on stderr, and exits
zero. Annotations are where Java puts its routes, so this harness can check
declarations and cannot check routes. Route extraction is fixture-tested
instead, and nothing here should be read as a compiler agreeing with it.

    python benchmarks/differential/run_java_differential.py --root path/to/src
    python benchmarks/differential/run_java_differential.py --jdk-sources

`--jdk-sources` extracts `java.base` from the running JDK's `lib/src.zip`,
which is 3,064 files of real Java that every JDK installation already has.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.analyzers.java_lexical import (
    declared_types,
    package_name,
    tokenize,
)

# `non-sealed` is the one modifier carrying a hyphen. Omitting it made this
# parser miss `DynamicConstantDesc` and report the reader as having invented
# a class that is plainly declared in the source.
DECLARATION = re.compile(
    r"^(?P<indent>\s*)(?P<modifiers>(?:[a-z]+(?:-[a-z]+)?\s+)*)"
    r"(?P<kind>@interface|interface|class|enum|record)\s+(?P<name>[A-Za-z_$][\w$]*)"
)


def strip_block_comments(text: str) -> str:
    """Remove block comments while preserving line count."""

    out: list[str] = []
    index = 0
    while index < len(text):
        start = text.find("/*", index)
        if start < 0:
            out.append(text[index:])
            break
        out.append(text[index:start])
        end = text.find("*/", start + 2)
        if end < 0:
            break
        out.append("\n" * text.count("\n", start, end))
        index = end + 2
    return "".join(out)


def reference_types(printed: str) -> set[str]:
    """Package-qualified type names, as `javac -Xprint` reported them."""

    found: set[str] = set()
    package = ""
    stack: list[tuple[str, int]] = []
    depth = 0
    for raw in strip_block_comments(printed).splitlines():
        line = raw.split("//")[0]
        if line.startswith("package "):
            package = line[len("package ") :].rstrip(";").strip()
            stack, depth = [], 0
            continue
        match = DECLARATION.match(line)
        if match:
            owner = ".".join(name for name, _ in stack)
            qualified = f"{owner}.{match.group('name')}" if owner else match.group("name")
            found.add(f"{package}.{qualified}" if package else qualified)
            depth += line.count("{") - line.count("}")
            stack.append((match.group("name"), depth))
            continue
        depth += line.count("{") - line.count("}")
        while stack and stack[-1][1] > depth:
            stack.pop()
    return found


def our_types(root: Path) -> set[str]:
    """Package-qualified type names, as this engine reads them.

    Local classes are excluded because they are not members: a class declared
    inside a method body is reachable by no qualified name, and `javac -Xprint`
    does not print one either.
    """

    found: set[str] = set()
    for path in sorted(root.rglob("*.java")):
        tokens = tokenize(path.read_text(encoding="utf-8", errors="replace"))
        package = package_name(tokens)
        for item in declared_types(tokens):
            if item.local:
                continue
            found.add(f"{package}.{item.name}" if package else item.name)
    return found


def extract_jdk_sources(destination: Path) -> Path:
    """Unpack `java.base` from the running JDK's source archive."""

    java_home = Path(sys.executable).parent
    candidates = [
        Path(shutil.which("javac") or "").resolve().parent.parent / "lib" / "src.zip",
        java_home / "lib" / "src.zip",
    ]
    archive = next((item for item in candidates if item.is_file()), None)
    if archive is None:
        raise SystemExit("could not find lib/src.zip beside javac; pass --root instead")
    with zipfile.ZipFile(archive) as bundle:
        names = [
            name
            for name in bundle.namelist()
            if name.startswith("java.base/") and name.endswith(".java")
        ]
        for name in names:
            target = destination / name[len("java.base/") :]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read(name))
    print(f"extracted {len(names):,} files from {archive}")
    return destination


def run_javac(root: Path, patch_module: str | None) -> str:
    sources = sorted(str(item) for item in root.rglob("*.java"))
    if not sources:
        raise SystemExit(f"no .java files under {root}")
    with tempfile.TemporaryDirectory() as scratch:
        listing = Path(scratch) / "sources.txt"
        listing.write_text("\n".join(sources), encoding="utf-8")
        command = ["javac", "-Xprint", "-d", str(Path(scratch) / "classes")]
        if patch_module:
            command += ["--patch-module", f"{patch_module}={root}"]
        command.append(f"@{listing}")
        completed = subprocess.run(  # noqa: S603
            command, capture_output=True, text=True, check=False
        )
    if not completed.stdout.strip():
        raise SystemExit(f"javac printed nothing:\n{completed.stderr[:2000]}")
    return completed.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Directory of .java files.")
    parser.add_argument(
        "--jdk-sources",
        action="store_true",
        help="Use java.base from the running JDK's lib/src.zip.",
    )
    parser.add_argument(
        "--patch-module",
        help="Module to patch, required when recompiling packages the JDK owns.",
    )
    parser.add_argument("--show", type=int, default=15, help="Disagreements to print.")
    arguments = parser.parse_args()

    scratch: tempfile.TemporaryDirectory[str] | None = None
    if arguments.jdk_sources:
        scratch = tempfile.TemporaryDirectory()
        root = extract_jdk_sources(Path(scratch.name))
        patch = arguments.patch_module or "java.base"
    elif arguments.root:
        root = arguments.root
        patch = arguments.patch_module
    else:
        parser.error("pass --root or --jdk-sources")

    printed = run_javac(root, patch)
    reference = reference_types(printed)
    ours = our_types(root)
    missing = sorted(reference - ours)
    invented = sorted(ours - reference)

    print(f"reference types: {len(reference):,}   ours: {len(ours):,}")
    print(f"we missed: {len(missing):,}   we invented: {len(invented):,}")
    for name in missing[: arguments.show]:
        print("  MISSED  ", name)
    for name in invented[: arguments.show]:
        print("  INVENTED", name)
    if missing or invented:
        print(
            "\nA disagreement is not a verdict. Check the source before changing "
            "the reader: on the first run of this harness every reported "
            "invention was the reference parser's fault."
        )
    if scratch is not None:
        scratch.cleanup()
    return 1 if (missing or invented) else 0


if __name__ == "__main__":
    raise SystemExit(main())
