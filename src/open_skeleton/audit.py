# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Flag claims that are probably true and probably misleading.

Every wrong answer this engine has produced was a true statement in the wrong
frame. Flask's test suite registers sixteen routes, and reporting them as the
served surface was accurate about each one and wrong about the system. A dict
of room names really is a mutable container, and calling it a queue described
a constant as a channel. A callback parameter really is a receiver, and
counting it as platform API filled a dependency census with local variables.

None of those were caught by a test, because each analyzer was correct in
isolation. They were caught by reading output from a repository nobody here
had written — which does not scale, and which is why this exists.

These checks look for the *shape* of that mistake rather than for any
particular instance of it: a finding that rests only on test files, a category
carried by a single file, a category that never has file-level evidence at
all. None of them proves a claim is wrong. Each one marks a place where being
wrong would look exactly like this, which is where a maintainer should read
before publishing a number.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from open_skeleton.policy import describes_the_product

# Claims that are a statement about the repository rather than about any file
# in it. Their receipts are census receipts by construction, so the
# no-file-evidence check would fire on every repository forever.
CENSUS_CATEGORIES = frozenset(
    {
        "delivery_automation",
        "testing_census",
        "testing_gap",
        "unsafe_surface",
        "auth_control_census",
        "dependency_inventory",
        "language_census",
        # The checked-out commit is a property of the repository, not of any
        # file in it, so its receipt is a census receipt by construction and
        # this check fired on every git repository ever analyzed -- five of
        # five here. A gate that always fails is not a gate: an agent wired
        # to `audit --strict` would reject every work order for a reason no
        # change could ever clear, and the first fix anyone reaches for is
        # turning the gate off.
        "checked_out_revision",
    }
)
# Categories that describe what a system does in production. A finding in one
# of these resting only on test files is describing the suite, not the system.
PRODUCTION_CATEGORIES = frozenset(
    {
        "http_route",
        "storage_schema",
        "process_local_state",
        "security_boundary",
        "third_party_origin",
        "hardcoded_endpoint",
        "schema_migration",
        "external_calls",
        "auth_control",
        # Outbound HTTP and browser storage describe what the running
        # program does. moonshot-mates reported "tests/rendered-html.test.mjs
        # contains 1 fetch call sites" as its top HTTP-interface finding --
        # a request the suite makes, presented as the surface the product
        # talks to. That is the Flask-routes-in-the-test-suite mistake this
        # module was written for, and the category was simply not listed.
        "http_client_inventory",
        "browser_storage",
        # The rest of what a running program does. These were unlisted for
        # no reason anyone recorded, and the list is load-bearing
        # configuration wearing the costume of a constant: the check that
        # missed a test suite's `fetch` was correct and simply never asked
        # about that category. Each of these was measured across six
        # repositories before being added and flags nothing today, so they
        # cost no noise and cover the case when it arrives.
        "application_entry",
        "caught_exception",
        "configuration_read",
        "error_surface",
        "exception_type",
        "failure_surface",
        "panic_site",
        "process_termination",
        "public_api",
        "storage",
        "trait_implementation",
        "ui_state",
    }
)
# Below this many claims a category cannot be judged: one claim from one file
# is not a pattern, it is a claim.
MIN_CLAIMS_FOR_SHAPE = 4
SINGLE_FILE_SHARE = 0.9
# A file carrying at least this share of the repository's evidenced claims is
# simply where the code lives, and its dominance of any one category is
# expected rather than suspect.
BROAD_FILE_SHARE = 0.25


@dataclass(frozen=True, slots=True)
class Finding:
    """One place the output has the shape of a past mistake."""

    check: str
    category: str
    detail: str
    claim_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "category": self.category,
            "detail": self.detail,
            "claim_ids": list(self.claim_ids),
            "claims": len(self.claim_ids),
        }


def _paths_for(
    claim: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]
) -> tuple[set[str], bool]:
    """File paths backing a claim, and whether any receipt names a file at all."""

    paths: set[str] = set()
    for evidence_id in claim.get("supporting_evidence", ()) or ():
        record = evidence_by_id.get(str(evidence_id))
        if record is None:
            continue
        path = str(record.get("path", ""))
        if path and path not in {".", ""} and not path.startswith("@"):
            paths.add(path)
    return paths, bool(paths)


def audit_claims(
    claims: tuple[dict[str, Any], ...],
    evidence: tuple[dict[str, Any], ...],
    files: tuple[dict[str, Any], ...],
) -> tuple[Finding, ...]:
    """Report claim groups shaped like a mistake this engine has made before."""

    evidence_by_id = {str(item["evidence_id"]): item for item in evidence}
    role_by_path = {str(item["path"]): str(item.get("role", "")) for item in files}

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for claim in claims:
        by_category[str(claim.get("category", ""))].append(claim)

    # How much of the repository's evidenced output each file carries, so a
    # category's concentration can be judged against the file's own weight.
    repository_wide: Counter[str] = Counter()
    total_sourced = 0
    for claim in claims:
        paths, has_file = _paths_for(claim, evidence_by_id)
        if has_file:
            total_sourced += 1
            for path in paths:
                repository_wide[path] += 1

    findings: list[Finding] = []
    for category, group in sorted(by_category.items()):
        sourced: list[tuple[dict[str, Any], set[str]]] = []
        unsourced = 0
        for claim in group:
            paths, has_file = _paths_for(claim, evidence_by_id)
            if has_file:
                sourced.append((claim, paths))
            else:
                unsourced += 1

        # A category whose every claim rests on a repository-wide census has no
        # file behind any of it. That is how an absence gets counted as
        # evidence of presence, which has happened three times here.
        #
        # Categories that are a census by construction are exempt. "No CI
        # workflow exists under .github/workflows" is a statement about the
        # repository, and there is no file it could name without inventing
        # one. Flagging them fired on five of six repositories the first time
        # this ran across a set, always on the same three categories -- and a
        # check that reports the same thing everywhere teaches the reader to
        # skip it, which costs more than the check was worth.
        if group and unsourced == len(group) and category not in CENSUS_CATEGORIES:
            findings.append(
                Finding(
                    check="no-file-evidence",
                    category=category,
                    detail=(
                        f"all {len(group):,} claims rest on repository-wide census "
                        "receipts, so none names a file. A presence probe over this "
                        "category counts absences as evidence."
                    ),
                    claim_ids=tuple(str(item.get("claim_id", "")) for item in group),
                )
            )

        if category in PRODUCTION_CATEGORIES and sourced:
            # Any file that exercises the system rather than being it, which
            # since the `harness` role means benchmarks and examples too. This
            # check was written for exactly that error and then missed it,
            # because it named one role instead of asking the question: a
            # benchmark opening a SQLite connection was filed as this
            # repository's storage behaviour and audited clean.
            not_the_product = [
                claim
                for claim, paths in sourced
                if paths
                and not any(describes_the_product(role_by_path.get(path)) for path in paths)
            ]
            if not_the_product:
                findings.append(
                    Finding(
                        check="test-only-evidence",
                        category=category,
                        detail=(
                            f"{len(not_the_product):,} of {len(sourced):,} claims rest only "
                            "on files that exercise this system rather than being it. A "
                            "finding about production behaviour evidenced solely by the "
                            "suite or a harness describes the suite or the harness."
                        ),
                        claim_ids=tuple(str(item.get("claim_id", "")) for item in not_the_product),
                    )
                )

        if len(sourced) >= MIN_CLAIMS_FOR_SHAPE:
            counts: Counter[str] = Counter()
            for _claim, paths in sourced:
                for path in paths:
                    counts[path] += 1
            path, hits = counts.most_common(1)[0]
            # Concentration alone is not suspicious. A monolithic service keeps
            # every route in one module, and flagging that would fire on most
            # repositories and teach a reader to skip this report. What is
            # suspicious is a category concentrated in a file that carries
            # little else — that is a pattern matching one file's idiom rather
            # than a property of the code.
            share_of_repository = repository_wide.get(path, 0) / max(1, total_sourced)
            if hits / len(sourced) >= SINGLE_FILE_SHARE and share_of_repository < BROAD_FILE_SHARE:
                findings.append(
                    Finding(
                        check="single-file-category",
                        category=category,
                        detail=(
                            f"{hits:,} of {len(sourced):,} claims come from `{path}`, which "
                            f"carries only {share_of_repository:.0%} of this repository's "
                            "evidenced claims overall. A category concentrated in a file "
                            "that carries little else is usually a pattern matching that "
                            "file's idiom rather than a property of the code."
                        ),
                        claim_ids=tuple(str(item.get("claim_id", "")) for item, _ in sourced),
                    )
                )

    return tuple(findings)
