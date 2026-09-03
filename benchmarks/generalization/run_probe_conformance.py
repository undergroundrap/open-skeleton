# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Put every marker the profile looks for in one repository, then look for them.

The gap census ranks what this engine never answered, and across 28
repositories it reported 430 such probes. Every one turned out to be the
corpus's silence rather than the engine's: no repository there holds a
Dockerfile, a Terraform plan, or a payment client, so "absent in 28 of 28"
was true and said nothing about whether the probe works.

Widening the corpus is the obvious answer and it is slow, partial, and never
finished -- there is always one more integration nobody happens to use. So
this asks the question directly instead. The profile declares every marker it
searches for; this builds a repository containing one of each and reports
which probes still find nothing. A failure here cannot be a corpus artifact,
because the marker is present by construction.

The fixture is generated *from the profile*, never written by hand. That is
the property that makes it safe: a fixture written by hand is a guess at what
the engine should find, drifts the moment a probe changes, and quietly becomes
a repository this engine is fitted to. One derived from the declarations
cannot disagree with them.

What this proves is narrow and worth stating. A probe that fires here can see
its marker in the simplest form that marker takes. It does not follow that
extraction is correct, that a real repository spells it this way, or that the
finding built on top of it is right. This measures reachability, which is the
floor rather than the ceiling.

Three outcomes, and only one is a defect:

  matched      the marker was placed and the probe found it
  missed       the marker is in the snapshot and the probe found nothing
  excluded     the marker was placed and policy refused to read it, which is
               correct for a secret and worth seeing rather than counting

    python benchmarks/generalization/run_probe_conformance.py
    python benchmarks/generalization/run_probe_conformance.py --keep <dir>

Exit status is non-zero when any probe missed a marker that was read.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository
from open_skeleton.spec import build_spec, load_profile

PROFILE = Path(__file__).resolve().parents[2] / "src/open_skeleton/spec/profiles/standard.json"

# Kinds whose presence is a file, a dependency, or an import, and can
# therefore be placed. `claim_category` is deliberately absent: a category
# exists only when an analyzer reads real code and decides something, and
# manufacturing an example would test the fixture rather than the engine.
PLACEABLE = ("path_glob", "dependency_name", "import_target")


def _concrete(pattern: str) -> str:
    """A real name matching a glob, with the wildcards spent on something.

    `*.tf` becomes `main.tf` and `k8s/**` becomes `k8s/generated.yaml`. The
    filler is arbitrary and must stay arbitrary: choosing names that look like
    a real project would make this a fixture about that project.
    """

    name = pattern.replace("**", "generated").replace("*", "main").replace("?", "x")
    return name.strip("/") or "placed"


def _walk(node: dict, found: list[tuple[str, str, str]]) -> None:
    for key in ("probes", "candidates"):
        for probe in node.get(key) or []:
            kind = str(probe.get("kind"))
            for term in probe.get("terms") or ():
                found.append((kind, str(term), str(probe.get("name") or "")))
    for child in node.get("children") or ():
        _walk(child, found)


def declared() -> list[tuple[str, str, str]]:
    payload = json.loads(PROFILE.read_text(encoding="utf-8"))
    found: list[tuple[str, str, str]] = []
    for section in payload.get("sections") or ():
        _walk(section, found)
    return found


def build_fixture(root: Path, markers: list[tuple[str, str, str]]) -> dict[str, str]:
    """Place one of every marker, and report which path each term became."""

    # An empty ignore file, so the repository states that it declares its own
    # generated directories. Without it a probe naming `build/` or `dist/` is
    # refused by name, and the refusal would be read here as a miss.
    (root / ".gitignore").write_text(
        "# generated directories are declared here\n", encoding="utf-8"
    )

    placed: dict[str, str] = {}
    npm: list[str] = []
    pypi: list[str] = []
    imports_python: list[str] = []
    imports_node: list[str] = []

    for kind, term, _name in markers:
        if kind == "path_glob":
            relative = _concrete(term)
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text(f"placed for probe term {term}\n", encoding="utf-8")
            placed[f"{kind}:{term}"] = relative
        elif kind == "dependency_name":
            named = _concrete(term)
            # Declared in both ecosystems rather than guessed between them. A
            # name says little about its registry, and a wrong guess would
            # report a working probe as broken.
            npm.append(named)
            pypi.append(named)
            placed[f"{kind}:{term}"] = named
        elif kind == "import_target":
            named = _concrete(term)
            imports_python.append(named)
            imports_node.append(named)
            placed[f"{kind}:{term}"] = named

    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "probe-conformance-fixture",
                "version": "0.0.0",
                "dependencies": dict.fromkeys(sorted(set(npm)), "^1.0.0"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "probe-conformance-fixture"\n'
        'version = "0.0.0"\n'
        "dependencies = [\n" + "".join(f'    "{name}",\n' for name in sorted(set(pypi))) + "]\n",
        encoding="utf-8",
    )
    # One import per file, and only names Python can actually spell.
    #
    # Both halves of that were learned the hard way. The first version wrote
    # every import into one module and turned `@opentelemetry/api` into
    # `import @opentelemetry.api`, which is a syntax error -- so the reader
    # skipped the whole file, no import edge existed, and all 101 probes came
    # back empty. That reads exactly like an entire probe kind being dead, and
    # it was a bug in this fixture. One file per import means a name this
    # generator spells wrongly costs one probe instead of all of them.
    for index, name in enumerate(sorted(set(imports_python))):
        spelled = name.replace("-", ".").replace("/", ".")
        if not all(part.isidentifier() for part in spelled.split(".") if part):
            continue
        (root / f"imports_{index}.py").write_text(f"import {spelled}\n", encoding="utf-8")
    (root / "uses_imports.js").write_text(
        "".join(
            f'const m{index} = require("{name}");\n'
            for index, name in enumerate(sorted(set(imports_node)))
            if not name.startswith("http")
        ),
        encoding="utf-8",
    )
    # A CDN is not imported by any module; a page loads it in a script tag,
    # which is the only place a repository ever holds one.
    urls = sorted({name for name in imports_node if name.startswith("http")})
    (root / "index.html").write_text(
        "<!doctype html>\n<html><head>\n"
        + "".join(f'<script src="{url}/library.js"></script>\n' for url in urls)
        + "</head><body></body></html>\n",
        encoding="utf-8",
    )
    return placed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=Path, help="Write the fixture here and leave it in place.")
    parser.add_argument("--show", type=int, default=30, help="Missed probes printed.")
    arguments = parser.parse_args()

    markers = [item for item in declared() if item[0] in PLACEABLE]
    unplaceable = [item for item in declared() if item[0] not in PLACEABLE]

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(arguments.keep) if arguments.keep else Path(scratch) / "fixture"
        root.mkdir(parents=True, exist_ok=True)
        placed = build_fixture(root, markers)

        snapshot = scan_repository(root)
        read = {item.path for item in snapshot.files}
        excluded = {str(item.path) for item in snapshot.exclusions}

        ledger = EvidenceLedger(Path(scratch) / "evidence.sqlite3")
        ledger.save_snapshot(snapshot)
        ledger.save_analysis(analyze_snapshot(snapshot))
        document = build_spec(ledger, load_profile())

    matched: set[tuple[str, str]] = set()
    zero: set[tuple[str, str]] = set()
    for section in document.to_dict().get("sections") or ():
        for probe in section.get("probes") or ():
            for term in probe.get("terms") or ():
                key = (str(probe.get("kind")), str(term))
                if int(probe.get("match_count") or 0) > 0:
                    matched.add(key)
                else:
                    zero.add(key)

    missed: list[tuple[str, str]] = []
    refused: list[tuple[str, str]] = []
    for kind, term, _name in markers:
        key = (kind, term)
        if key in matched or key not in zero:
            continue
        relative = placed.get(f"{kind}:{term}", "")
        if kind == "path_glob" and relative not in read:
            refused.append((kind, term))
        else:
            missed.append((kind, term))

    placed_keys = {(kind, term) for kind, term, _ in markers}
    total = len(placed_keys)
    # A term declared only as a `candidate` is never evaluated as a section
    # probe, so it is neither matched nor missed. Counted rather than left as
    # a silent difference between two numbers that do not add up.
    never_queried = placed_keys - matched - zero
    print("\n## Probe conformance on a fixture holding every placeable marker\n")
    print(f"markers placed: {total}   matched: {len(matched & placed_keys)}")
    print(f"missed: {len(missed)}   refused by policy: {len(refused)}")
    print(f"placed but never queried as a section probe: {len(never_queried)}")
    print(f"not placeable (need real code): {len({(k, t) for k, t, _ in unplaceable})}\n")

    by_kind = Counter(kind for kind, _ in missed)
    print("### Missed, by kind\n")
    if not missed:
        print("  none")
    for kind, count in by_kind.most_common():
        print(f"  {count:4}  {kind}")
    print()
    for kind, term in sorted(missed)[: arguments.show]:
        print(f"  MISSED {kind}: {term}  (placed as {placed.get(f'{kind}:{term}')})")
    if refused:
        print("\n### Refused by scan policy, which is correct for a secret\n")
        for kind, term in sorted(refused)[:12]:
            print(f"  {kind}: {term}")
    print()
    _ = excluded
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
