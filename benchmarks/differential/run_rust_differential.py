# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Compare this engine's Rust reading against `syn`, a real Rust parser.

The TypeScript differential found five defects on its first run, including a
tokenizer fault that silently erased every declaration after a regex
containing a quote. The Rust analyzer is lexical for the same reasons and has
had the same amount of outside scrutiny, which is none: until this existed it
was checked only against fixtures written here and one repository.

The reference is a small helper crate under `benchmarks/differential/rustref`
that parses one file with `syn` and prints the items and trait
implementations it finds. `syn` is what procedural macros are written
against, so it is the same reader the Rust ecosystem itself trusts.

Both directions are informative and they mean different things:

* **`syn` names an item this engine misses** — a declaration the
  specification will never mention.
* **This engine names one `syn` does not** — a fabrication, which is the
  worse of the two, because a reader cannot tell it from a fact.

The helper is a development dependency and is never required to analyze a
repository. Without a Rust toolchain this exits zero and says so.

    cargo build --release --manifest-path benchmarks/differential/rustref/Cargo.toml
    python benchmarks/differential/run_rust_differential.py --root some/rust/project
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.analyzers.rust_lexical import (
    ENUM_CONSTRUCTORS,
    _call_sites,
    _declared_items,
    _trait_implementations,
    tokenize,
)

SKIP_DIRECTORIES = {"target", ".git", "node_modules"}
MAX_FILES = 400


@dataclass
class Disagreement:
    path: str
    missing: tuple[str, ...] = ()
    invented: tuple[str, ...] = ()


@dataclass
class Report:
    compared: int = 0
    unparsed: int = 0
    missing: list[Disagreement] = field(default_factory=list)
    invented: list[Disagreement] = field(default_factory=list)


def _helper(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit if explicit.exists() else None
    here = Path(__file__).resolve().parent / "rustref" / "target" / "release"
    for name in ("rustref.exe", "rustref"):
        candidate = here / name
        if candidate.exists():
            return candidate
    return None


def _reference(helper: Path, source: Path) -> dict[str, set[str]] | None:
    result = subprocess.run(  # noqa: S603 — fixed binary, path argument
        [str(helper), str(source)], capture_output=True, check=False, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    payload = json.loads(result.stdout)
    if not payload.get("parsed"):
        return None
    return {
        # `syn` keeps the escape on a raw identifier, printing `r#async`
        # where this engine records the name itself. Both are right about
        # the same function, so the harness normalizes rather than making
        # either side adopt the other's convention.
        "decls": {name.removeprefix("r#") for name in payload.get("names", [])},
        "impls": {f"{item['owner']}:{item['trait']}" for item in payload.get("impls", [])},
        # `Some(x)` is an `ExprCall` to `syn` and a construction to this
        # engine, which deliberately records no call for it. Normalising
        # here keeps a stated design choice out of the defect column.
        "calls": {
            name.removeprefix("r#")
            for name in payload.get("calls", [])
            if name not in ENUM_CONSTRUCTORS
        },
    }


def _ours(source: Path) -> dict[str, set[str]]:
    tokens = tokenize(source.read_text(encoding="utf-8", errors="replace"))
    return {
        "decls": {name for _, name, _ in _declared_items(tokens)},
        "impls": {f"{owner}:{trait}" for owner, trait, _ in _trait_implementations(tokens)},
        "calls": {name for name, _ in _call_sites(tokens)},
    }


def _candidates(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*.rs")):
        if len(found) >= MAX_FILES:
            break
        if any(part in SKIP_DIRECTORIES for part in path.parts):
            continue
        found.append(path)
    return found


def compare(root: Path, helper: Path) -> Report:
    report = Report()
    for source in _candidates(root):
        reference = _reference(helper, source)
        if reference is None:
            report.unparsed += 1
            continue
        report.compared += 1
        ours = _ours(source)
        relative = str(source.relative_to(root)).replace("\\", "/")
        # Both families are compared: implementations say what a type can be
        # used as, declarations say what the crate contains at all.
        #
        # The reference's `names` is the right side of this, not its `decls`.
        # `decls` holds only what a module declares directly, while
        # `_declared_items` counts every `fn` keyword including the methods
        # inside an `impl` block. Comparing against `decls` reported fifty-seven
        # files of fabrication in ripgrep, all of them real methods -- a
        # definition mismatch that looked exactly like a defect.
        absent = (
            (reference["impls"] - ours["impls"])
            | (reference["decls"] - ours["decls"])
            | (reference["calls"] - ours["calls"])
        )
        extra = (
            (ours["impls"] - reference["impls"])
            | (ours["decls"] - reference["decls"])
            | (ours["calls"] - reference["calls"])
        )
        if absent:
            report.missing.append(Disagreement(relative, missing=tuple(sorted(absent))))
        if extra:
            report.invented.append(Disagreement(relative, invented=tuple(sorted(extra))))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Rust project to compare.")
    parser.add_argument("--helper", type=Path, default=None, help="Path to the rustref binary.")
    parser.add_argument(
        "--strict", action="store_true", help="Exit non-zero on any invented implementation."
    )
    arguments = parser.parse_args()

    helper = _helper(arguments.helper)
    if helper is None:
        print(
            "rustref not built; skipping. Build it with "
            "`cargo build --release --manifest-path benchmarks/differential/rustref/Cargo.toml`."
        )
        return 0

    report = compare(arguments.root.expanduser().resolve(strict=True), helper)
    print(f"files compared: {report.compared:,}  (syn could not parse {report.unparsed:,})")
    print(f"  implementations we miss:   {len(report.missing):,} file(s)")
    print(f"  implementations we invent: {len(report.invented):,} file(s)")

    if report.invented:
        print("\nINVENTED — reported here and absent from the parse:")
        for item in report.invented[:20]:
            print(f"  {item.path}: {', '.join(item.invented)}")
    if report.missing:
        print("\nMISSING — parsed by syn and never reported:")
        for item in report.missing[:20]:
            print(f"  {item.path}: {', '.join(item.missing)}")

    return 1 if arguments.strict and report.invented else 0


if __name__ == "__main__":
    raise SystemExit(main())
