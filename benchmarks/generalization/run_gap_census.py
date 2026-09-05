# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Ask the engine what it could not answer, across many repositories at once.

Every other instrument here asks whether an answer is right. This asks which
questions came back empty, and in how many repositories -- which is the only
version of that question that cannot be answered by fitting.

The ranking is the whole design. A concern absent from one repository is a
fact about that repository: `starlette` serves no routes because it is the
library other people declare routes with, and reporting that is correct. The
same concern absent from forty repositories in a row is a fact about this
engine. So nothing here is ranked by how bad a single repository looks, and a
gap earns attention only by breadth. A reader who wants to improve one
repository's specification is holding the wrong tool.

Three questions, in the order they are worth acting on:

1. probes that matched nothing in *any* repository. Either no analyzer can
   emit that category, or nothing in the corpus has the property. Both are
   worth knowing and breadth separates them: across enough unrelated
   repositories, a category that never appears is usually unreachable rather
   than universally absent.
2. languages present in repositories and read by no analyzer, counted by how
   many repositories keep files in them. An absence resting on an unread file
   reads exactly like a checked absence, which is the failure this engine
   exists to prevent.
3. references that name something and resolve to nothing, bucketed by the
   shape of the reference rather than its text, because the text is a fact
   about one repository and the shape is a fact about a language.

Concern absences are reported as what they are, which is mostly the shape of
the corpus. "Payment Processing absent in 70 of 70 repositories" is true of 70
Python libraries that take no payments and says nothing about this engine, and
ranking it above the things that do would be fitting with extra steps.

A false absence cannot be found by asking the section, because `absent` is
defined as every one of its probes matching nothing -- there is no such thing
as a section that came back absent while its own evidence said otherwise. It
can only be found with evidence the probe did not use, which is what the first
list does: a probe naming a file the snapshot holds, and a probe naming a
dependency the manifest declares.

    python benchmarks/generalization/run_gap_census.py --root .venv/Lib/site-packages
    python benchmarks/generalization/run_gap_census.py --repo one --repo two

Exit status is zero. This measures what to build next; it is not a gate, and
wiring it into CI would make an honest report of an unusual repository a red
build.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import traceback
from collections import Counter
from collections.abc import Sequence
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.ledger import EvidenceLedger
from open_skeleton.scanner import scan_repository
from open_skeleton.spec import build_spec, load_profile
from open_skeleton.spec.render import _languages_no_analyzer_read

SKIP_SUFFIXES = (".dist-info", ".egg-info", ".egg")


def targets(root: Path | None, repos: list[Path]) -> list[Path]:
    found = list(repos)
    if root is not None:
        found += sorted(
            item
            for item in root.iterdir()
            if item.is_dir()
            and item.name != "__pycache__"
            and not item.name.endswith(SKIP_SUFFIXES)
        )
    return found


def _reference_shape(reference: str) -> str:
    """The kind of reference this is, which is a fact about a language.

    Bucketed rather than counted verbatim: `./model` and `./schema` are one
    gap and two strings, and ranking the strings would rank repositories.
    """

    if reference.startswith(("./", "../")):
        return "relative path (./x)"
    if reference.startswith(("super::", "self::")) or reference in {"super", "self", "crate"}:
        return "module-relative (super::x)"
    if reference.startswith("crate::"):
        return "crate-root (crate::x)"
    if reference.startswith("@"):
        return "scoped package (@scope/x)"
    if "::" in reference:
        return "namespaced (a::b)"
    if "." in reference:
        return "dotted (a.b)"
    return "bare name"


def _missed_globs(zero: set[tuple[str, str]], paths: Sequence[str]) -> set[tuple[str, str]]:
    """Probes that found nothing while the thing they name is right there.

    "Containerization absent in 28 of 28 repositories" reads like a hole in
    this engine and is not: no repository in that corpus holds a Dockerfile,
    so the absence is the corpus's truth. Without separating the two, the
    census ranks the shape of whatever corpus it was pointed at, which is
    fitting with extra steps.

    A probe that names a file can be checked against the file census
    directly, and then the two stop looking alike. The glob matched nothing
    and the file is absent: correct. The glob matched nothing and the file is
    sitting in the snapshot: a defect, with no judgement required.
    """

    missed: set[tuple[str, str]] = set()
    for kind, term in zero:
        if kind != "path_glob":
            continue
        for path in paths:
            if fnmatch(path, term) or fnmatch(PurePosixPath(path).name, term):
                missed.add((kind, term))
                break
    return missed


# Files that declare a dependency by name. A probe naming one of those names
# and matching no edge is the dependency check's version of a glob that names
# a file sitting in the snapshot.
MANIFESTS = (
    "package.json",
    "cargo.toml",
    "pyproject.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gemfile",
    "composer.json",
    "requirements.txt",
)
MAX_MANIFEST_BYTES = 400_000


def _declared_dependencies(target: Path, paths: Sequence[str]) -> str:
    """The text of every manifest in the repository, lowercased, joined.

    Text rather than a parsed dependency list on purpose. Every manifest
    format states its dependencies differently and this check does not need to
    know which is which: it asks whether a name the probe was looking for is
    written down anywhere a dependency is written down.
    """

    found: list[str] = []
    for path in paths:
        if PurePosixPath(path).name.casefold() not in MANIFESTS:
            continue
        item = target / path
        try:
            if item.stat().st_size > MAX_MANIFEST_BYTES:
                continue
            found.append(item.read_text(encoding="utf-8", errors="replace").casefold())
        except OSError:
            continue
    return "\n".join(found)


def _missed_dependencies(zero: set[tuple[str, str]], manifests: str) -> set[tuple[str, str]]:
    """Dependency probes that found nothing the manifest declares outright.

    The term is matched as a quoted or delimited name rather than a substring,
    because `ava` inside `java` is not a dependency on `ava` and a census that
    reported it would be ranking coincidence. A glob term contributes its stem
    -- `@aws-sdk/*` looks for `@aws-sdk/`.
    """

    missed: set[tuple[str, str]] = set()
    if not manifests:
        return missed
    for kind, term in zero:
        if kind != "dependency_name":
            continue
        # The separator goes with the star: `@aws-sdk/*` looks for
        # `"@aws-sdk/`, and keeping the slash in the stem made every scoped
        # package silently unmatchable.
        stem = term.casefold().split("*", 1)[0].rstrip("-_/")
        if len(stem) < 3:
            continue
        for boundary in (f'"{stem}"', f"'{stem}'", f'"{stem}/', f"{stem} ="):
            if boundary in manifests:
                missed.add((kind, term))
                break
    return missed


class Census:
    def __init__(self) -> None:
        self.repositories = 0
        self.probe_missed: Counter[tuple[str, str]] = Counter()
        self.probe_zero: Counter[tuple[str, str]] = Counter()
        self.probe_matched: set[tuple[str, str]] = set()
        self.section_absent: Counter[str] = Counter()
        self.language_unread: Counter[str] = Counter()
        self.unresolved: Counter[str] = Counter()
        self.resolved_imports = 0
        self.total_imports = 0

    def add(self, target: Path, state: Path) -> None:
        snapshot = scan_repository(target)
        if not snapshot.files:
            return
        analysis = analyze_snapshot(snapshot)
        ledger = EvidenceLedger(state)
        ledger.save_snapshot(snapshot)
        ledger.save_analysis(analysis)
        document = build_spec(ledger, load_profile())
        payload = document.to_dict()
        self.repositories += 1

        # Counted once per repository. A term appears in several sections, so
        # incrementing per occurrence produced "56/28" -- a gap present in
        # twice as many repositories as were examined. The census is here to
        # rank by breadth, and a number that can exceed its own denominator
        # ranks nothing.
        zero_here: set[tuple[str, str]] = set()
        matched_here: set[tuple[str, str]] = set()
        for section in payload.get("sections") or []:
            probes = section.get("probes") or []
            if str(section.get("verdict")) == "absent":
                self.section_absent[str(section.get("title") or section.get("section_id"))] += 1
            for probe in probes:
                for term in probe.get("terms") or ():
                    key = (str(probe.get("kind")), str(term))
                    if int(probe.get("match_count") or 0) > 0:
                        matched_here.add(key)
                    else:
                        zero_here.add(key)
        self.probe_matched |= matched_here
        unanswered = zero_here - matched_here
        for key in unanswered:
            self.probe_zero[key] += 1
        paths = [item.path for item in snapshot.files]
        for key in _missed_globs(unanswered, paths):
            self.probe_missed[key] += 1
        for key in _missed_dependencies(unanswered, _declared_dependencies(target, paths)):
            self.probe_missed[key] += 1

        # A language no analyzer produced a record for, which is not the same
        # as one whose analyzer failed to parse it. Delegated rather than
        # reimplemented: the first version of this compared coverage labels to
        # file languages and reported Markdown and TypeScript as unread in
        # repositories whose analyzers had read both, because a coverage
        # record is keyed by analyzer and its label need not equal the
        # language of a file.
        for language, _count in _languages_no_analyzer_read(
            [item.to_dict() for item in snapshot.files],
            [item.to_dict() for item in analysis.symbols],
            [item.to_dict() for item in analysis.evidence],
        ):
            self.language_unread[language] += 1

        for edge in analysis.edges:
            if edge.relationship != "imports":
                continue
            self.total_imports += 1
            if edge.target_symbol_id:
                self.resolved_imports += 1
            else:
                self.unresolved[_reference_shape(edge.target_ref)] += 1

    def report(self, show: int) -> None:
        print(f"\n## Repositories examined: {self.repositories}\n")

        print(f"### Probes that missed something the repository holds ({len(self.probe_missed)})\n")
        print("The file the probe names is in the snapshot, or the dependency it")
        print("names is in the manifest, and the probe found nothing. No judgement")
        print("is needed about these: they are defects.\n")
        if not self.probe_missed:
            print("  none")
        for (kind, term), count in self.probe_missed.most_common(show):
            print(f"  {count:3}/{self.repositories}  {kind}: {term}")
        print()

        never = [
            (key, count) for key, count in self.probe_zero.items() if key not in self.probe_matched
        ]
        never.sort(key=lambda item: (-item[1], item[0]))
        print(f"### Probes that matched nothing in any repository ({len(never)})\n")
        print("A question this engine asks and has never once answered. Across")
        print("unrelated repositories, that is usually a category no analyzer can")
        print("emit rather than a property none of them has.\n")
        for (kind, term), count in never[:show]:
            print(f"  {count:3}/{self.repositories}  {kind}: {term}")

        print("\n### Concerns absent, which is mostly the shape of the corpus\n")
        print("A concern nothing in the corpus exercises reports absent everywhere,")
        print("and that is a fact about the corpus. It cannot be told apart from a")
        print("gap by asking the section: `absent` means every probe matched")
        print("nothing, so no section can report absent while its own evidence")
        print("disagrees. The list above is where a false absence shows up,")
        print("because it uses evidence the probe did not.\n")
        for title, count in self.section_absent.most_common(show):
            print(f"  {count:3}/{self.repositories}  {title}")

        print("\n### Languages present and read by no analyzer\n")
        if not self.language_unread:
            print("  none")
        for language, count in self.language_unread.most_common(show):
            print(f"  {count:3}/{self.repositories}  {language}")

        share = self.resolved_imports / max(1, self.total_imports)
        print("\n### References that name something and resolve to nothing\n")
        print(
            f"  {self.resolved_imports:,} of {self.total_imports:,} imports resolve ({share:.0%})\n"
        )
        for shape, count in self.unresolved.most_common(show):
            print(f"  {count:6,}  {shape}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Directory whose children are repositories.")
    parser.add_argument(
        "--repo", type=Path, action="append", default=[], help="One repository; repeatable."
    )
    parser.add_argument("--show", type=int, default=14, help="Rows printed per question.")
    arguments = parser.parse_args()

    paths = targets(arguments.root, arguments.repo)
    if not paths:
        parser.error("pass --root or at least one --repo")

    census = Census()
    with tempfile.TemporaryDirectory() as scratch:
        for index, target in enumerate(paths):
            try:
                census.add(target, Path(scratch) / f"{index}.sqlite3")
            except Exception:  # noqa: BLE001 - a census reports failures, it does not raise
                print(f"CRASH {target.name}", flush=True)
                print("   " + traceback.format_exc().strip().splitlines()[-1], flush=True)
    census.report(arguments.show)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
