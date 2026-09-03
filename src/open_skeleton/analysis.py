# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
import re
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from open_skeleton.analyzers.base import Analyzer
from open_skeleton.analyzers.csharp_lexical import CSharpLexicalAnalyzer
from open_skeleton.analyzers.documented_measurements import DocumentedMeasurementAnalyzer
from open_skeleton.analyzers.hum_semantic_index import HumSemanticIndexAnalyzer
from open_skeleton.analyzers.java_lexical import JavaLexicalAnalyzer
from open_skeleton.analyzers.powershell_lexical import PowerShellLexicalAnalyzer
from open_skeleton.analyzers.project_metadata import ProjectMetadataAnalyzer
from open_skeleton.analyzers.python_ast import PythonAstAnalyzer
from open_skeleton.analyzers.rust_lexical import RustLexicalAnalyzer
from open_skeleton.analyzers.sql_schema import SqlSchemaAnalyzer
from open_skeleton.analyzers.typescript_lexical import TypeScriptLexicalAnalyzer
from open_skeleton.analyzers.workflow_triggers import WorkflowTriggerAnalyzer
from open_skeleton.http_targets import local_request_path
from open_skeleton.ids import stable_id
from open_skeleton.models import (
    AnalysisResult,
    ClaimRecord,
    CoverageRecord,
    EdgeRecord,
    EvidenceRecord,
    Snapshot,
    SymbolRecord,
    utc_now,
)
from open_skeleton.policy import scoped_category
from open_skeleton.resolution import resolve_import_targets

PIPELINE_VERSION = "deterministic-pipeline/v1"
EXPONENTIAL_BASE_PATTERN = re.compile(
    r"exponentiates base (?P<base>[0-9]+(?:\.[0-9]+)?) by `(?P<exponent>[^`]+)`"
)


# Words too common to mean two expressions describe the same quantity. Without
# this, `retry_count` and `user_count` would be read as one variable.
GENERIC_EXPONENT_TOKENS = frozenset(
    {"count", "n", "i", "x", "value", "index", "idx", "num", "number", "total", "size", "len"}
)


def _exponent_subject(expression: str) -> frozenset[str]:
    """The quantity an exponent expression refers to, as comparable tokens.

    Two bases only diverge against each other when they are raised to the same
    thing, and the same thing is rarely spelled the same way twice:
    `player.ascension_count`, `args.ascensions` and `ascensions` are one
    quantity written three ways. Comparing the expressions literally finds no
    pair; pooling every base in the repository compares curves that share
    nothing.

    Tokens are split on non-letters, lowercased, and singularised crudely by
    dropping a trailing `s`. Words that carry no subject of their own are
    dropped, so `retry_count` and `user_count` do not become the same thing on
    the strength of `count`.
    """

    tokens = {
        part.rstrip("s") or part
        for part in re.split(r"[^A-Za-z]+", expression.casefold())
        if len(part) > 2
    }
    return frozenset(tokens - GENERIC_EXPONENT_TOKENS)


def _append_orphan_candidates(
    snapshot: Snapshot,
    *,
    created_at: str,
    symbols: tuple[SymbolRecord, ...],
    edges: tuple[EdgeRecord, ...],
    evidence: tuple[EvidenceRecord, ...],
    claims: list[ClaimRecord],
) -> tuple[EvidenceRecord, ...]:
    """Add conservative static orphan candidates using the resolved import census."""

    python_modules = [
        item
        for item in symbols
        if item.kind == "module"
        and item.analyzer.startswith("python-ast/")
        # A stub is read by a type checker, never imported by a module, so
        # every one of them is unreachable by this census by construction.
        # Reading `.pyi` files gained `attr` seven orphan candidates that were
        # all its public type declarations -- a true statement about the
        # import graph and a false one about dead code.
        and not item.path.endswith(".pyi")
    ]
    imports = [item for item in edges if item.relationship == "imports"]
    evidence_by_symbol = {
        item.symbol: item
        for item in evidence
        if item.evidence_kind == "module" and item.symbol is not None
    }
    file_roles = {item.path: item.role for item in snapshot.files}
    entrypoint_paths = {
        invalidation.removeprefix("file:")
        for claim in claims
        if claim.category == "application_entry"
        for invalidation in claim.invalidation_keys
        if invalidation.startswith("file:")
    }

    def module_is_imported(module: str) -> bool:
        candidates = {module}
        parts = module.split(".")
        if len(parts) > 1:
            candidates.add(".".join(parts[1:]))
        return any(
            target == candidate or target.startswith(f"{candidate}.")
            for candidate in candidates
            for edge in imports
            for target in (edge.target_ref.lstrip("."),)
        )

    imported_modules = {
        module.qualified_name
        for module in python_modules
        if module_is_imported(module.qualified_name)
    }
    census = EvidenceRecord(
        evidence_id=stable_id(
            "evidence",
            (snapshot.snapshot_id, ".", "static_import_census", PIPELINE_VERSION),
        ),
        snapshot_id=snapshot.snapshot_id,
        path=".",
        start_line=None,
        end_line=None,
        symbol=None,
        evidence_kind="static_import_census",
        excerpt_sha256=snapshot.snapshot_id,
        analyzer=PIPELINE_VERSION,
        created_at=created_at,
    )
    added = False
    for module in python_modules:
        if (
            module.path.endswith("/__init__.py")
            or module.path == "__init__.py"
            or file_roles.get(module.path) != "source"
            or module.path in entrypoint_paths
            or module.qualified_name in imported_modules
        ):
            continue
        parent = module.qualified_name.rsplit(".", 1)[0] if "." in module.qualified_name else ""
        imported_siblings = [
            sibling
            for sibling in python_modules
            if sibling.qualified_name.rsplit(".", 1)[0] == parent
            and sibling.qualified_name in imported_modules
        ]
        if not imported_siblings:
            continue
        module_evidence = evidence_by_symbol.get(module.qualified_name)
        if module_evidence is None:
            continue
        text = (
            f"{module.path} has no resolved inbound static Python import while "
            f"{len(imported_siblings)} sibling modules do; treat it as an orphan candidate, "
            "not a deletion instruction."
        )
        claims.append(
            ClaimRecord(
                claim_id=stable_id(
                    "claim",
                    (snapshot.snapshot_id, "orphan_candidate", text, PIPELINE_VERSION),
                ),
                snapshot_id=snapshot.snapshot_id,
                claim=text,
                category="orphan_candidate",
                status="inferred",
                confidence=0.95,
                importance="medium",
                produced_by=PIPELINE_VERSION,
                created_at=created_at,
                supporting_evidence=(module_evidence.evidence_id, census.evidence_id),
                invalidation_keys=(
                    f"file:{module.path}",
                    f"module:{module.qualified_name}",
                    "python:import-graph",
                ),
                alternative_hypotheses=(
                    (
                        "The module may be loaded dynamically, invoked directly, imported by an "
                        "external consumer, or retained intentionally as a reference."
                    ),
                ),
            )
        )
        added = True
    return evidence + ((census,) if added else ())


def _append_mathematical_conflicts(
    snapshot: Snapshot,
    *,
    created_at: str,
    evidence: tuple[EvidenceRecord, ...],
    claims: list[ClaimRecord],
) -> tuple[EvidenceRecord, ...]:
    # Grouped by the exponent, not pooled across the repository. Two bases
    # only diverge against each other when they are raised to the same thing:
    # `1.25 ** tier` and `2 ** retries` are unrelated curves, and comparing
    # them would report a ratio nothing computes. The original code compared
    # every base to every other and was correct only because the one
    # repository it ran on raised both to the same variable.
    measured: list[tuple[float, frozenset[str], str, ClaimRecord]] = []
    for claim in claims:
        if claim.category != "exponential_scaling":
            continue
        match = EXPONENTIAL_BASE_PATTERN.search(claim.claim)
        if match:
            written = match.group("exponent")
            measured.append(
                (float(match.group("base")), _exponent_subject(written), written, claim)
            )
    shared: list[tuple[str, list[tuple[float, ClaimRecord]]]] = []
    for _base, subject, written, _claim in measured:
        if not subject:
            continue
        group = [
            (other_base, other_claim)
            for other_base, other_subject, _, other_claim in measured
            if other_subject & subject
        ]
        if len({item[0] for item in group}) >= 2:
            shared.append((written, group))
    if not shared:
        return evidence
    exponent, scaling_claims = shared[0]
    distinct_bases = sorted({base for base, _ in scaling_claims})

    file_records = {item.path: item for item in snapshot.files}
    comment_receipts: list[EvidenceRecord] = []
    for path, file_record in file_records.items():
        if file_record.language != "Python":
            continue
        source_path = snapshot.root / Path(path)
        payload = source_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != file_record.sha256:
            continue
        source = payload.decode("utf-8", errors="strict")
        for line_number, line in enumerate(source.splitlines(keepends=True), start=1):
            lowered = line.casefold()
            if "never trivial" not in lowered and "wall forever" not in lowered:
                continue
            comment_receipts.append(
                EvidenceRecord(
                    evidence_id=stable_id(
                        "evidence",
                        (
                            snapshot.snapshot_id,
                            path,
                            line_number,
                            "exponential_comment",
                            PIPELINE_VERSION,
                        ),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    path=path,
                    start_line=line_number,
                    end_line=line_number,
                    symbol=None,
                    evidence_kind="documentation_assertion",
                    excerpt_sha256=hashlib.sha256(line.encode("utf-8")).hexdigest(),
                    analyzer=PIPELINE_VERSION,
                    created_at=created_at,
                )
            )
    lower, upper = distinct_bases[0], distinct_bases[-1]
    if lower <= 0 or upper <= lower:
        return evidence
    supporting = tuple(
        sorted(
            {
                evidence_id
                for base, claim in scaling_claims
                if base in {lower, upper}
                for evidence_id in claim.supporting_evidence
            }
        )
    )
    # The divergence is a fact about the arithmetic and holds with or without
    # anything written about it. This used to be reported only when a comment
    # contained "never trivial" or "wall forever" -- two phrases from the game
    # this pipeline was first written against -- so two competing growth curves
    # in any other repository were computed, compared, and then discarded for
    # want of a sentence nobody else would write.
    divergence = (
        f"Exponential bases {upper:g} and {lower:g} are both raised to `{exponent}`, so "
        f"their ratio is ({upper:g}/{lower:g})^N and grows without bound as that value "
        "does. Whether anything caps it is not decided here."
    )
    text = (
        f"{divergence} Source comments assert that the gap stays fixed, which those "
        "formulas contradict."
        if comment_receipts
        else divergence
    )
    claims.append(
        ClaimRecord(
            claim_id=stable_id(
                "claim", (snapshot.snapshot_id, "mathematical_conflict", text, PIPELINE_VERSION)
            ),
            snapshot_id=snapshot.snapshot_id,
            claim=text,
            category="mathematical_conflict",
            # A conflict needs two sides. Without a documented assertion this
            # is an unchallenged property of the arithmetic, and calling it a
            # conflict would invent the other side.
            status="conflict" if comment_receipts else "verified",
            confidence=1.0,
            importance="high" if comment_receipts else "medium",
            produced_by=PIPELINE_VERSION,
            created_at=created_at,
            supporting_evidence=supporting,
            contradicting_evidence=tuple(item.evidence_id for item in comment_receipts),
            # Keyed on the formulas as well as on anything contradicting them.
            # Deriving these from the comments alone left a claim with no
            # invalidation key at all once the comment stopped being required,
            # so editing the arithmetic it describes would never have retired
            # it.
            invalidation_keys=tuple(
                sorted(
                    {f"file:{item.path}" for item in comment_receipts}
                    | {
                        key
                        for _, claim in scaling_claims
                        for key in claim.invalidation_keys
                        if key.startswith("file:")
                    }
                )
            ),
            alternative_hypotheses=(
                (
                    "A cap on the exponent, or another term growing faster, could bound the "
                    "ratio in practice; neither follows from the compared exponentials alone."
                ),
            ),
        )
    )
    return evidence + tuple(comment_receipts)


def _append_testing_census(
    snapshot: Snapshot,
    *,
    created_at: str,
    evidence: tuple[EvidenceRecord, ...],
    claims: list[ClaimRecord],
) -> tuple[EvidenceRecord, ...]:
    test_files = tuple(item for item in snapshot.files if item.role == "test")
    if test_files:
        return evidence
    census = EvidenceRecord(
        evidence_id=stable_id(
            "evidence", (snapshot.snapshot_id, ".", "test_role_census", PIPELINE_VERSION)
        ),
        snapshot_id=snapshot.snapshot_id,
        path=".",
        start_line=None,
        end_line=None,
        symbol=None,
        evidence_kind="snapshot_census",
        excerpt_sha256=snapshot.snapshot_id,
        analyzer=PIPELINE_VERSION,
        created_at=created_at,
    )
    text = (
        "The bounded snapshot contains no files classified as conventional test files; "
        "operator or validation scripts may still exist under non-test paths."
    )
    claims.append(
        ClaimRecord(
            claim_id=stable_id(
                "claim", (snapshot.snapshot_id, "testing_census", text, PIPELINE_VERSION)
            ),
            snapshot_id=snapshot.snapshot_id,
            claim=text,
            category="testing_census",
            status="verified",
            confidence=1.0,
            importance="medium",
            produced_by=PIPELINE_VERSION,
            created_at=created_at,
            verified_at=created_at,
            supporting_evidence=(census.evidence_id,),
            invalidation_keys=("snapshot:file-set",),
            alternative_hypotheses=(
                "Tests may use project-specific naming conventions not recognized by the scanner.",
            ),
        )
    )
    return (*evidence, census)


# Below this a helper is used by one suite and is that suite's own setup, not
# shared test infrastructure anyone needs to know about.
MIN_SUITES_FOR_A_SHARED_HELPER = 2
# Enough to name where test data comes from without listing a module's imports.
MAX_NAMED_HELPERS = 6


def _append_shared_test_helpers(
    snapshot: Snapshot,
    *,
    created_at: str,
    symbols: tuple[SymbolRecord, ...],
    edges: tuple[EdgeRecord, ...],
    evidence: tuple[EvidenceRecord, ...],
    claims: list[ClaimRecord],
) -> tuple[EvidenceRecord, ...]:
    """Functions defined in the suite that several test modules call.

    "Where does the test data come from" is one of the first questions asked
    of an unfamiliar repository, and the answer is a handful of builders the
    suite shares. Both halves were already in the ledger -- the call edges and
    the symbols -- and nothing put them together.

    Callees are resolved against symbols defined in test-role files, which is
    what separates a shared builder from `assertEqual` and `TemporaryDirectory`.
    Without that filter the most-called names in any suite are the assertion
    methods, which say nothing about the repository.
    """

    test_paths = {item.path for item in snapshot.files if str(item.role) == "test"}
    if not test_paths:
        return evidence
    # A name defined in more than one test module is ambiguous: five suites
    # each declaring their own `_claim` helper are not five callers of one
    # shared thing, and reporting them as such invents infrastructure. The
    # same rule already governs capability attribution, where accepting the
    # ambiguous case cost more in wrong answers than it bought in coverage.
    declared: dict[str, set[str]] = defaultdict(set)
    for symbol in symbols:
        if symbol.path in test_paths and symbol.kind in {"function", "method"}:
            declared[symbol.qualified_name.rsplit(".", 1)[-1]].add(symbol.path)
    defined = {name: next(iter(paths)) for name, paths in declared.items() if len(paths) == 1}
    callers: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.relationship != "calls" or edge.source_path not in test_paths:
            continue
        if edge.target_ref in defined:
            callers[edge.target_ref].add(edge.source_path)
    shared = sorted(
        (
            (name, defined[name], len(paths))
            for name, paths in callers.items()
            if len(paths) >= MIN_SUITES_FOR_A_SHARED_HELPER
        ),
        key=lambda item: (-item[2], item[0]),
    )
    if not shared:
        return evidence

    census = EvidenceRecord(
        evidence_id=stable_id(
            "evidence", (snapshot.snapshot_id, ".", "shared_test_helpers", PIPELINE_VERSION)
        ),
        snapshot_id=snapshot.snapshot_id,
        path=".",
        start_line=None,
        end_line=None,
        symbol=None,
        evidence_kind="snapshot_census",
        excerpt_sha256=snapshot.snapshot_id,
        analyzer=PIPELINE_VERSION,
        created_at=created_at,
    )
    named = ", ".join(
        f"`{name}` ({path}, {count:,} module(s))"
        for name, path, count in shared[:MAX_NAMED_HELPERS]
    )
    remainder = len(shared) - min(len(shared), MAX_NAMED_HELPERS)
    tail = f", and {remainder:,} more" if remainder else ""
    text = (
        f"{len(shared):,} helper(s) defined in the test suite are called by more than one "
        f"test module: {named}{tail}. These are where this suite's fixtures come from."
    )
    claims.append(
        ClaimRecord(
            claim_id=stable_id("claim", (snapshot.snapshot_id, "testing", text, PIPELINE_VERSION)),
            snapshot_id=snapshot.snapshot_id,
            claim=text,
            category="testing",
            status="verified",
            confidence=1.0,
            importance="medium",
            produced_by=PIPELINE_VERSION,
            created_at=created_at,
            verified_at=created_at,
            supporting_evidence=(census.evidence_id,),
            invalidation_keys=("snapshot:file-set",),
            alternative_hypotheses=(
                (
                    "Callees are matched by their last name segment. A helper whose name is "
                    "declared in more than one test module is skipped as ambiguous rather "
                    "than attributed to one of them."
                ),
            ),
        )
    )
    return (*evidence, census)


def _append_route_documentation_conflicts(
    snapshot: Snapshot,
    *,
    created_at: str,
    claims: list[ClaimRecord],
) -> None:
    source = next((item for item in claims if item.category == "http_route_inventory"), None)
    documented = next(
        (item for item in claims if item.category == "documented_http_route_inventory"),
        None,
    )
    if source is None or documented is None:
        return
    source_count = re.search(r"declares (\d+) HTTP route", source.claim)
    documented_count = re.search(r"document (\d+) distinct HTTP", documented.claim)
    if (
        not source_count
        or not documented_count
        or source_count.group(1) == documented_count.group(1)
    ):
        return
    source_value = int(source_count.group(1))
    documented_value = int(documented_count.group(1))
    text = (
        f"Markdown API tables document {documented_value} endpoints while Python source "
        f"declares {source_value} HTTP route handlers."
    )
    claims.append(
        ClaimRecord(
            claim_id=stable_id(
                "claim", (snapshot.snapshot_id, "api_documentation_drift", text, PIPELINE_VERSION)
            ),
            snapshot_id=snapshot.snapshot_id,
            claim=text,
            category="api_documentation_drift",
            status="conflict",
            confidence=1.0,
            importance="high",
            produced_by=PIPELINE_VERSION,
            created_at=created_at,
            supporting_evidence=documented.supporting_evidence,
            contradicting_evidence=source.supporting_evidence,
            invalidation_keys=tuple(
                sorted({*documented.invalidation_keys, *source.invalidation_keys})
            ),
        )
    )


CLIENT_ROUTE_PATH = re.compile(r"^(?P<target>\S+) (?:is requested|begins a request path)")
SERVED_ROUTE_PATH = re.compile(
    r"^(?:(?P<method>[A-Z]+) )?(?P<path>/\S*) is (?:registered|handled|served)"
)


def _append_client_route_reconciliation(
    snapshot: Snapshot,
    *,
    created_at: str,
    claims: list[ClaimRecord],
) -> None:
    """Join a path a client requests to a route this snapshot serves.

    Both halves already existed and nothing connected them. A dashboard's
    document carried "GET /api/adapter is registered as a route in server"
    in one section and "/api/adapter is requested by web.app; the server
    side of this call is whichever route matches it" two sections later --
    the hedge is correct in general and needlessly weak when the answer is
    in the same document.

    The join is deliberately narrow. Only an exact path match counts, and
    the claim says a route matching it is registered *in this snapshot*
    rather than that the call reaches it: nothing here proves the client is
    configured to talk to this server, and a monorepo can hold two services
    that spell a path the same way.
    """

    served: dict[str, ClaimRecord] = {}
    for claim in claims:
        if claim.category != "http_route":
            continue
        match = SERVED_ROUTE_PATH.match(claim.claim)
        if match:
            served.setdefault(match.group("path"), claim)

    requested: list[tuple[str, ClaimRecord]] = []
    for claim in claims:
        if claim.category != "http_client_route":
            continue
        match = CLIENT_ROUTE_PATH.match(claim.claim)
        path = local_request_path(match.group("target")) if match is not None else None
        if path is not None:
            requested.append((path, claim))

    for path, client_claim in requested:
        route_claim = served.get(path)
        if route_claim is None:
            continue
        text = (
            f"{path} is requested by a client in this repository and a route matching it is "
            "registered here too, so both sides of that call are in this snapshot. That the "
            "client is configured to reach this server is not decided here."
        )
        claims.append(
            ClaimRecord(
                claim_id=stable_id(
                    "claim",
                    (snapshot.snapshot_id, "client_route_reconciliation", text, PIPELINE_VERSION),
                ),
                snapshot_id=snapshot.snapshot_id,
                claim=text,
                category="client_route_reconciliation",
                status="verified",
                confidence=1.0,
                importance="high",
                produced_by=PIPELINE_VERSION,
                created_at=created_at,
                verified_at=created_at,
                supporting_evidence=tuple(
                    dict.fromkeys(
                        (*client_claim.supporting_evidence, *route_claim.supporting_evidence)
                    )
                ),
                invalidation_keys=tuple(
                    sorted({*client_claim.invalidation_keys, *route_claim.invalidation_keys})
                ),
                alternative_hypotheses=(
                    (
                        "A path spelled the same way in two services is the same string and "
                        "not necessarily the same endpoint; this joins them by exact path "
                        "within one snapshot and resolves no configuration."
                    ),
                ),
            )
        )


# Module specifiers that name no package. A relative or absolute path is a
# file in this repository, and `node:` is how a JavaScript runtime spells its
# own standard library. Neither can appear in a manifest, so comparing them
# against one manufactures a conflict at the highest severity this engine
# assigns -- skill-cue reported "Operator scripts import /", from
# `import { skillCards } from "../../src/lib/skillCards"`.
NODE_BUILTIN_NAMES = frozenset(
    {
        "assert",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "crypto",
        "dgram",
        "dns",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "querystring",
        "readline",
        "stream",
        "string_decoder",
        "timers",
        "tls",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "worker_threads",
        "zlib",
    }
)


def _dependency_name(target: str) -> str | None:
    """The package a module specifier names, or None when it names none.

    Written per ecosystem rather than per language because the rule differs:
    Python separates with dots and JavaScript with slashes, so splitting a
    specifier on `.` turned `../../src/lib/skillCards` into `/`.
    """

    specifier = target.strip()
    if not specifier or specifier.startswith((".", "/", "#")):
        return None
    if specifier.startswith("node:"):
        return None
    if specifier.startswith("@"):
        # A scoped npm package is two segments; one alone names nothing.
        parts = specifier.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    if "/" in specifier:
        return specifier.split("/", 1)[0]
    if specifier in NODE_BUILTIN_NAMES:
        return None
    return specifier.split(".", 1)[0]


def _append_dependency_conflicts(
    snapshot: Snapshot,
    *,
    created_at: str,
    edges: tuple[EdgeRecord, ...],
    evidence: tuple[EvidenceRecord, ...],
    claims: list[ClaimRecord],
) -> None:
    declared = {
        item.target_ref.casefold().replace("_", "-")
        for item in edges
        if item.relationship == "declares_dependency"
    }
    if not declared:
        return
    local_names = {
        part.casefold().replace("_", "-")
        for file_record in snapshot.files
        if file_record.language == "Python"
        for part in (*Path(file_record.path).parts[:-1], Path(file_record.path).stem)
    }
    standard = {item.casefold().replace("_", "-") for item in sys.stdlib_module_names}
    requirements_evidence = tuple(
        item.evidence_id for item in evidence if item.evidence_kind == "requirements_manifest"
    )
    imports: dict[str, set[str]] = {}
    for edge in edges:
        if edge.relationship != "imports" or not edge.source_path.startswith("scripts/"):
            continue
        top_level = _dependency_name(edge.target_ref)
        if top_level is None:
            continue
        normalized = top_level.casefold().replace("_", "-")
        if (
            not normalized
            or normalized in standard
            or normalized in local_names
            or normalized in declared
            or edge.evidence_id is None
        ):
            continue
        imports.setdefault(normalized, set()).add(edge.evidence_id)
    for dependency, import_evidence in sorted(imports.items()):
        text = (
            f"Operator scripts import {dependency}, but the analyzed requirements manifests "
            f"do not declare {dependency}."
        )
        claims.append(
            ClaimRecord(
                claim_id=stable_id(
                    "claim", (snapshot.snapshot_id, "dependency_drift", text, PIPELINE_VERSION)
                ),
                snapshot_id=snapshot.snapshot_id,
                claim=text,
                category="dependency_drift",
                status="conflict",
                confidence=0.98,
                importance="high",
                produced_by=PIPELINE_VERSION,
                created_at=created_at,
                supporting_evidence=tuple(sorted(import_evidence)),
                contradicting_evidence=requirements_evidence,
                invalidation_keys=("snapshot:file-set", "python:import-graph"),
                alternative_hypotheses=(
                    (
                        "The dependency may be installed manually, transitively, or by an untracked "
                        "environment definition."
                    ),
                ),
            )
        )


def _attribute_claim_yield(
    results: Sequence[AnalysisResult],
) -> tuple[CoverageRecord, ...]:
    """Count, per analyzer, how many parsed files actually produced a claim.

    Coverage alone answers "did the file parse", which overstates how much an
    analyzer understood. A claim is attributed to every real file its supporting
    receipts point at; repository-wide census receipts (path ``.``) name no file
    and are excluded, so a single "no CI workflow exists" claim cannot make a
    whole language look productive.
    """

    attributed: list[CoverageRecord] = []
    for result in results:
        receipts = {item.evidence_id: item.path for item in result.evidence}
        claimed_paths = {
            path
            for claim in result.claims
            for evidence_id in (*claim.supporting_evidence, *claim.contradicting_evidence)
            for path in (receipts.get(evidence_id),)
            if path is not None and path not in {".", ""} and not path.startswith("@")
        }
        for record in result.coverage:
            attributed.append(
                replace(
                    record,
                    claimed_files=min(len(claimed_paths), record.analyzed_files),
                )
            )
    return tuple(attributed)


AnalysisEventCallback = Callable[[str, int, int], None]


def _scope_claims_by_evidence_role(
    snapshot: Snapshot,
    claims: Sequence[ClaimRecord],
    evidence: Sequence[EvidenceRecord],
) -> list[ClaimRecord]:
    """File a claim by the role of the files it actually rests on.

    Two analyzers did this internally and the rest did not, so the same defect
    kept being found and fixed one reader at a time: routes, then schemas, then
    durable storage, each corrected where it was noticed. The reader that
    reported a suite's `fetch` as a product's outbound HTTP surface carries a
    comment saying the rule exists elsewhere and never crossed over.

    It belongs here because this is the only place that sees every claim
    against every file's role, so an analyzer written next week inherits the
    behaviour without knowing the rule exists. Deciding by the evidence rather
    than by a file also reaches the aggregate claims, which name no single
    path: the Rust panic census is one claim over many receipts, and in a
    repository whose only Rust is a differential reference implementation,
    every one of those receipts is a benchmark.

    Conservative in three ways. A claim resting on any product source stays a
    product claim, since one real site makes the statement true of the system.
    A claim whose evidence spans two exercising roles at once is left alone
    rather than assigned to whichever looks more likely. And a receipt that
    resolves to no known file leaves the claim untouched, so a gap in
    bookkeeping cannot silently demote a finding.
    """

    role_by_path = {str(item.path): str(item.role) for item in snapshot.files}
    path_by_evidence = {str(item.evidence_id): str(item.path) for item in evidence}

    scoped: list[ClaimRecord] = []
    for claim in claims:
        supporting = tuple(claim.supporting_evidence or ())
        paths = {path_by_evidence.get(item) for item in supporting}
        if not paths or None in paths:
            scoped.append(claim)
            continue
        roles = {role_by_path.get(path or "") for path in paths}
        if len(roles) != 1:
            scoped.append(claim)
            continue
        role = str(next(iter(roles)) or "")
        category = scoped_category(claim.category, role)
        if category == claim.category:
            scoped.append(claim)
            continue
        # A fixture's shape and a benchmark's error handling are worth
        # recording and are not headlines about the system, so a re-filed
        # claim does not keep a product finding's prominence.
        importance = "medium" if claim.importance == "high" else claim.importance
        scoped.append(replace(claim, category=category, importance=importance))
    return scoped


def _merge_duplicate_claims(claims: list[ClaimRecord]) -> list[ClaimRecord]:
    """Fold claims sharing an identifier into one, keeping every receipt.

    Two sites can state the same fact. Rust's ``#[cfg]`` is the ordinary case:
    a trait is implemented once for Windows and once for everything else, both
    impls are real, and exactly one compiles per platform. The claim text is
    identical, so both records carry the same identifier.

    Storing them separately was impossible -- the ledger keys claims by that
    identifier -- and storing them in sequence deleted the first receipt while
    writing the second, so the run reported 635 claims and persisted 634 with
    one citation pointing at whichever impl happened to be written last.
    Merging the evidence keeps both lines, which is also the more useful
    reading: it shows a reader that the fact holds by two different routes.
    """

    merged: dict[str, ClaimRecord] = {}
    for claim in sorted(claims, key=lambda item: item.claim_id):
        existing = merged.get(claim.claim_id)
        if existing is None:
            merged[claim.claim_id] = claim
            continue
        merged[claim.claim_id] = replace(
            existing,
            supporting_evidence=_union(existing.supporting_evidence, claim.supporting_evidence),
            contradicting_evidence=_union(
                existing.contradicting_evidence, claim.contradicting_evidence
            ),
            invalidation_keys=_union(existing.invalidation_keys, claim.invalidation_keys),
            alternative_hypotheses=_union(
                existing.alternative_hypotheses, claim.alternative_hypotheses
            ),
        )
    return list(merged.values())


def _union(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    """Both sequences, order preserved, without repeats."""

    return tuple(dict.fromkeys((*first, *second)))


def build_analyzers(hum_index: Sequence[Path] | Path | None = None) -> tuple[Analyzer, ...]:
    """The analyzers a run consults, in the order their claims are merged.

    Declared at module scope and annotated with the protocol so conformance is
    checked rather than assumed. As a tuple built inside the run function the
    annotation had nowhere to attach, and `Analyzer` went unimported by
    anything -- a contract five classes were expected to satisfy and none was
    measured against.
    """

    return (
        PythonAstAnalyzer(),
        TypeScriptLexicalAnalyzer(),
        RustLexicalAnalyzer(),
        JavaLexicalAnalyzer(),
        ProjectMetadataAnalyzer(),
        SqlSchemaAnalyzer(),
        PowerShellLexicalAnalyzer(),
        CSharpLexicalAnalyzer(),
        DocumentedMeasurementAnalyzer(),
        WorkflowTriggerAnalyzer(),
        HumSemanticIndexAnalyzer(hum_index),
    )


def analyze_snapshot(
    snapshot: Snapshot,
    *,
    hum_index: Sequence[Path] | Path | None = None,
    on_event: AnalysisEventCallback | None = None,
) -> AnalysisResult:
    """Run deterministic semantic adapters and merge their immutable outputs."""

    started = time.perf_counter()
    created_at = utc_now()
    results = []
    for analyzer in build_analyzers(hum_index):
        result = analyzer.analyze(snapshot)
        results.append(result)
        if on_event is not None:
            on_event(
                result.analyzer_version,
                round((time.perf_counter() - started) * 1000),
                len(result.claims),
            )

    symbols = tuple(item for result in results for item in result.symbols)
    edges = tuple(item for result in results for item in result.edges)
    evidence = tuple(item for result in results for item in result.evidence)
    claims = [item for result in results for item in result.claims]
    coverage = _attribute_claim_yield(results)

    evidence = _append_orphan_candidates(
        snapshot,
        created_at=created_at,
        symbols=symbols,
        edges=edges,
        evidence=evidence,
        claims=claims,
    )
    _append_route_documentation_conflicts(
        snapshot,
        created_at=created_at,
        claims=claims,
    )
    _append_client_route_reconciliation(
        snapshot,
        created_at=created_at,
        claims=claims,
    )
    _append_dependency_conflicts(
        snapshot,
        created_at=created_at,
        edges=edges,
        evidence=evidence,
        claims=claims,
    )
    evidence = _append_mathematical_conflicts(
        snapshot,
        created_at=created_at,
        evidence=evidence,
        claims=claims,
    )
    evidence = _append_testing_census(
        snapshot,
        created_at=created_at,
        evidence=evidence,
        claims=claims,
    )
    evidence = _append_shared_test_helpers(
        snapshot,
        created_at=created_at,
        symbols=symbols,
        edges=edges,
        evidence=evidence,
        claims=claims,
    )

    for file_record in snapshot.files:
        if file_record.role not in {"source", "test"} or file_record.line_count < 1_000:
            continue
        # A long test file concentrates the suite, not the architecture.
        # Both are worth knowing and they answer different questions, so the
        # claim says which one it is rather than presenting them as one list.
        in_test = str(file_record.role) == "test"
        text = (
            f"{file_record.path} concentrates {file_record.line_count} lines of test code."
            if in_test
            else f"{file_record.path} is an architectural concentration point at "
            f"{file_record.line_count} lines."
        )
        evidence_id = stable_id(
            "inventory-evidence",
            (snapshot.snapshot_id, file_record.path, file_record.sha256, "file_concentration"),
        )
        inventory_evidence = EvidenceRecord(
            evidence_id=evidence_id,
            snapshot_id=snapshot.snapshot_id,
            path=file_record.path,
            start_line=1,
            end_line=file_record.line_count,
            symbol=None,
            evidence_kind="file_inventory",
            excerpt_sha256=file_record.sha256,
            analyzer=PIPELINE_VERSION,
            created_at=created_at,
        )
        evidence += (inventory_evidence,)
        claims.append(
            ClaimRecord(
                claim_id=stable_id(
                    "claim",
                    (snapshot.snapshot_id, "concentration", text, PIPELINE_VERSION),
                ),
                snapshot_id=snapshot.snapshot_id,
                claim=text,
                category="test_concentration" if in_test else "concentration",
                status="verified",
                confidence=1.0,
                importance="medium" if in_test else "high",
                produced_by=PIPELINE_VERSION,
                created_at=created_at,
                verified_at=created_at,
                supporting_evidence=(evidence_id,),
                invalidation_keys=(f"file:{file_record.path}",),
            )
        )

    return AnalysisResult(
        snapshot_id=snapshot.snapshot_id,
        analyzer_version=PIPELINE_VERSION,
        created_at=created_at,
        duration_ms=round((time.perf_counter() - started) * 1000),
        symbols=tuple(
            sorted(symbols, key=lambda item: (item.path, item.start_line, item.symbol_id))
        ),
        # Resolved after every analyzer has contributed, because a reference
        # is often satisfied by a symbol another reader declared: a TypeScript
        # module importing from a `.tsx` file crosses two analyzers, and
        # neither one alone holds both halves.
        edges=tuple(
            sorted(
                resolve_import_targets(snapshot.files, symbols, edges),
                key=lambda item: item.edge_id,
            )
        ),
        evidence=tuple(sorted(evidence, key=lambda item: item.evidence_id)),
        # Merge first, then re-file. Merging folds claims that share an
        # identifier and unions their receipts, so afterwards a claim's
        # evidence is complete and the role behind it can be read once. Doing
        # it the other way would ask the question of a partial answer, and
        # could collapse a source-evidenced claim into a re-filed one, since
        # the identifier was computed before the category changed.
        claims=tuple(
            _scope_claims_by_evidence_role(
                snapshot,
                _merge_duplicate_claims(claims),
                evidence,
            )
        ),
        coverage=coverage,
    )
