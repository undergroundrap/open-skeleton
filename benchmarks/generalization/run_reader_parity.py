# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Which readers record the things the other readers record?

Four languages gained a vocabulary reader one at a time, and each gap was
found only once a fixture in that language existed: Python had one, TypeScript
did not until zod was added, Rust did not until clap was added, Java did not
until `java.util.concurrent` was added. Each time the finding was the same
sentence with a different language in it, and each time it waited for a
fixture to be written.

That is a slow way to learn something structural. A named constant, a named
string and a closed set of values are declared in every language this engine
reads, and a reader either records them or does not. So this asks all of them
at once: it writes a few lines of ordinary source per language, runs the
pipeline, and reports which surfaces came back.

The snippets are deliberately plain. This is a conformance check against
surfaces the engine already claims to provide, not a test of how well any
reader handles real code -- a fixture measures that, and five of them do.
A blank cell is not proof of a defect either: a language may genuinely lack
the concept. It is a question worth asking, and the row says which reader to
ask it of.

    python benchmarks/generalization/run_reader_parity.py

Exit status is zero. This measures where readers disagree; it is not a gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.scanner import scan_repository

# One file per language, declaring the same three things in each language's
# ordinary spelling: a number worth tuning, a named string, and a closed set.
SNIPPETS: dict[str, tuple[str, str]] = {
    "Python": (
        "policy.py",
        (
            "import json\n"
            "\n"
            "MAX_RETRIES = 10\n"
            'SERVICE_NAME = "checkout"\n'
            'ALLOWED_METHODS = frozenset(["GET", "PUT", "HEAD"])\n'
            '__all__ = ["run"]\n'
            "\n"
            "def run(path):\n"
            '    raise ValueError("bad path")\n'
        ),
    ),
    "TypeScript": (
        "policy.ts",
        (
            'import fs from "node:fs";\n'
            "export const MAX_RETRIES: number = 10;\n"
            'export const SERVICE_NAME: string = "checkout";\n'
            'export type Method = "GET" | "PUT" | "HEAD";\n'
            'export function run(p: string) { throw new Error("bad path"); }\n'
        ),
    ),
    "Rust": (
        "policy.rs",
        (
            "use std::io;\n"
            "pub const MAX_RETRIES: u32 = 10;\n"
            'pub const SERVICE_NAME: &str = "checkout";\n'
            "pub enum Method { Get, Put, Head }\n"
            'pub fn run() -> Result<(), io::Error> { panic!("bad path") }\n'
        ),
    ),
    "Java": (
        "Policy.java",
        (
            "import java.util.List;\n"
            "public class Policy {\n"
            "    public static final int MAX_RETRIES = 10;\n"
            '    public static final String SERVICE_NAME = "checkout";\n'
            "    public void run() {\n"
            '        throw new IllegalStateException("bad path");\n'
            "    }\n"
            "}\n"
            "enum Method { GET, PUT, HEAD }\n"
        ),
    ),
    "C#": (
        "Policy.cs",
        (
            "using System;\n"
            "namespace Service;\n"
            "public class Policy {\n"
            "    public const int MaxRetries = 10;\n"
            '    public const string ServiceName = "checkout";\n'
            "    public void Run() {\n"
            '        throw new InvalidOperationException("bad path");\n'
            "    }\n"
            "}\n"
            "public enum Method { Get, Put, Head }\n"
        ),
    ),
    # Written the way the module Windows ships writes it, not the way the
    # language permits. The first version of this row used `Set-Variable
    # -Option Constant` and an `enum`, and `PSDesiredStateConfiguration`
    # contains zero of either across 25 files: it states limits as
    # `$script:MaxComponentDepth = 1024` and vocabularies as `ValidateSet`.
    # A conformance snippet written from the manual would have been satisfied
    # by a reader that finds nothing real.
    "PowerShell": (
        "policy.ps1",
        (
            "Import-Module Foo\n"
            "$script:MaxRetries = 10\n"
            "$script:ServiceName = 'checkout'\n"
            "function Set-Thing {\n"
            "    param(\n"
            "        [ValidateSet('GET', 'PUT', 'HEAD')]\n"
            "        [String] $Method\n"
            "    )\n"
            "    throw 'bad path'\n"
            "}\n"
            "Export-ModuleMember -Function Set-Thing\n"
        ),
    ),
}

VALUE_SURFACES = ("tunables", "string_constants", "collection_constants")

# A module exposes something, fails somehow, and depends on something. Each is
# as universal as a named constant, and each is reported by some readers and
# not others. The failure family is a set because the readers name it
# differently and that difference is not itself a gap: a Rust `panic!` and a
# Python `raise` are the same question answered in each language's own words.
FAILURE_CATEGORIES = frozenset(
    {
        "failure_surface",
        "error_surface",
        "exception_type",
        "panic_site",
        "caught_exception",
        "absorbed_failure",
    }
)
CLAIM_SURFACES = {
    "public surface": frozenset({"public_api"}),
    "failure surface": FAILURE_CATEGORIES,
}
EDGE_SURFACES = {"imports": "imports"}
SURFACES = (*VALUE_SURFACES, *CLAIM_SURFACES, *EDGE_SURFACES)


def surfaces_for(name: str, source: str) -> tuple[set[str], int]:
    """Which declared-value surfaces a reader produced, and how many symbols."""

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / name).write_text(source, encoding="utf-8")
        snapshot = scan_repository(root)
        if not snapshot.files:
            return set(), 0
        result = analyze_snapshot(snapshot)

    found: set[str] = set()
    for symbol in result.symbols:
        metadata = symbol.metadata or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        for surface in VALUE_SURFACES:
            if metadata.get(surface):
                found.add(surface)

    categories = {claim.category for claim in result.claims}
    for surface, wanted in CLAIM_SURFACES.items():
        if categories & wanted:
            found.add(surface)

    relationships = {edge.relationship for edge in result.edges}
    for surface, wanted_edge in EDGE_SURFACES.items():
        if wanted_edge in relationships:
            found.add(surface)
    return found, len(result.symbols)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()

    rows: list[tuple[str, set[str], int]] = []
    for language, (name, source) in SNIPPETS.items():
        found, symbols = surfaces_for(name, source)
        rows.append((language, found, symbols))

    if arguments.json:
        print(
            json.dumps(
                {language: sorted(found) for language, found, _ in rows},
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print("\n## Declared-value surfaces, by reader\n")
    header = f"| {'Language':12} | " + " | ".join(f"{item[:18]:18}" for item in SURFACES) + " |"
    print(header)
    print("|" + "|".join("-" * (len(part) + 2) for part in header.split("|")[1:-1]) + "|")
    for language, found, symbols in rows:
        cells = " | ".join(f"{'yes' if item in found else 'no':18}" for item in SURFACES)
        print(f"| {language:12} | {cells} |")
        _ = symbols

    missing = [
        (language, surface)
        for language, found, _ in rows
        for surface in SURFACES
        if surface not in found
    ]
    print(
        f"\n{len(missing)} reader/surface pair(s) record nothing. A blank is a question "
        "rather than a verdict: a language may not have the concept, and the row says "
        "which reader to ask.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
