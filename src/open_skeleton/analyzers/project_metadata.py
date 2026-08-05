# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import hashlib
import json
import re
import time
import tomllib
from pathlib import Path
from typing import Any

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

ANALYZER_NAME = "project-metadata"
ANALYZER_VERSION = "project-metadata/v1"
# A stylesheet reaches a third party through `@import`, `url()`, or a `<link>`
# href. The scheme is required so a bare domain in a comment is not a request.
STYLE_ORIGIN = re.compile(r"(?:https?:)?//(?P<host>[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,})")
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})  # noqa: S104
# Declared on inline SVG and XHTML elements as an identifier. Nothing is fetched
# from these, and treating them as egress would flag every icon in the tree.
NAMESPACE_HOSTS = frozenset(
    {
        "www.w3.org",
        "w3.org",
        "www.inkscape.org",
        "sodipodi.sourceforge.net",
        "creativecommons.org",
        "purl.org",
        "schema.org",
        "www.opengis.net",
        "xmlns.com",
    }
)
TAILWIND_PATTERN = re.compile(r"\btailwind(?:\s+css)?\b", re.IGNORECASE)
REQUIREMENT_PATTERN = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
DOCUMENTED_ROUTE_PATTERN = re.compile(
    r"\|\s*`?(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)`?\s*\|\s*`(/[^`|\s]*)`",
    re.IGNORECASE,
)
# The curly apostrophe is deliberate: documentation commonly writes "doesn't"
# with U+2019 rather than an ASCII quote.
NEGATION_PATTERN = re.compile(r"\b(?:no|not|without|doesn['’]?t)\b", re.IGNORECASE)  # noqa: RUF001


def _normalize_requirement(value: object) -> str | None:
    """Reduce a PEP 508 requirement string to its normalized distribution name."""

    if not isinstance(value, str):
        return None
    match = REQUIREMENT_PATTERN.match(value)
    if not match:
        return None
    return match.group(1).casefold().replace("_", "-")


def _pyproject_name(document: dict[str, Any]) -> str | None:
    project = document.get("project")
    if isinstance(project, dict) and isinstance(project.get("name"), str):
        return str(project["name"])
    return None


def _pyproject_dependencies(document: dict[str, Any]) -> dict[str, set[str]]:
    """Collect PEP 621 runtime and optional dependency names.

    Only the standard `[project]` table is read. Tool-specific tables such as
    Poetry's are deliberately out of scope until they have their own tests, so
    an unsupported layout reports zero rather than a partial guess.
    """

    runtime: set[str] = set()
    optional: set[str] = set()
    project = document.get("project")
    if not isinstance(project, dict):
        return {"runtime": runtime, "optional": optional}

    declared = project.get("dependencies")
    if isinstance(declared, list):
        runtime.update(name for name in map(_normalize_requirement, declared) if name is not None)

    extras = project.get("optional-dependencies")
    if isinstance(extras, dict):
        for group in extras.values():
            if isinstance(group, list):
                optional.update(
                    name for name in map(_normalize_requirement, group) if name is not None
                )
    return {"runtime": runtime, "optional": optional - runtime}


class ProjectMetadataAnalyzer:
    name = ANALYZER_NAME
    version = ANALYZER_VERSION

    def analyze(self, snapshot: Snapshot) -> AnalysisResult:
        started = time.perf_counter()
        created_at = utc_now()
        symbols: list[SymbolRecord] = []
        edges: list[EdgeRecord] = []
        evidence: list[EvidenceRecord] = []
        claims: list[ClaimRecord] = []
        failures: list[str] = []
        eligible = [
            item
            for item in snapshot.files
            if item.language == "Markdown"
            or Path(item.path).name.casefold()
            in {"package.json", "requirements.txt", "pyproject.toml"}
        ]
        analyzed_files = 0
        file_sources: dict[str, str] = {}

        for file_record in snapshot.files:
            path = snapshot.root / Path(file_record.path)
            try:
                payload = path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != file_record.sha256:
                    raise ValueError("content changed after snapshot")
                file_sources[file_record.path] = payload.decode("utf-8", errors="strict")
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                if file_record in eligible:
                    failures.append(f"{file_record.path}: {exc.__class__.__name__}: {exc}")

        def receipt(
            path: str,
            start_line: int | None,
            end_line: int | None,
            kind: str,
            symbol: str | None = None,
        ) -> EvidenceRecord:
            if path == ".":
                excerpt_hash = snapshot.snapshot_id
            else:
                source = file_sources[path]
                lines = source.splitlines(keepends=True)
                start = start_line or 1
                end = end_line or max(1, len(lines))
                excerpt_hash = hashlib.sha256(
                    "".join(lines[start - 1 : end]).encode("utf-8")
                ).hexdigest()
            record = EvidenceRecord(
                evidence_id=stable_id(
                    "evidence",
                    (
                        snapshot.snapshot_id,
                        path,
                        start_line,
                        end_line,
                        kind,
                        symbol,
                        ANALYZER_VERSION,
                    ),
                ),
                snapshot_id=snapshot.snapshot_id,
                path=path,
                start_line=start_line,
                end_line=end_line,
                symbol=symbol,
                evidence_kind=kind,
                excerpt_sha256=excerpt_hash,
                analyzer=ANALYZER_VERSION,
                created_at=created_at,
            )
            evidence.append(record)
            return record

        package_names: set[str] = set()
        manifest_receipts: list[str] = []
        for file_record in eligible:
            if file_record.path not in file_sources:
                continue
            manifest_name = Path(file_record.path).name.casefold()
            if file_record.language == "Markdown":
                analyzed_files += 1
                continue
            if manifest_name == "requirements.txt":
                manifest_evidence = receipt(
                    file_record.path,
                    1,
                    max(1, file_record.line_count),
                    "requirements_manifest",
                    file_record.path,
                )
                manifest_receipts.append(manifest_evidence.evidence_id)
                symbol_id = stable_id(
                    "symbol",
                    (
                        snapshot.snapshot_id,
                        file_record.path,
                        "requirements_manifest",
                        ANALYZER_VERSION,
                    ),
                )
                symbols.append(
                    SymbolRecord(
                        symbol_id=symbol_id,
                        snapshot_id=snapshot.snapshot_id,
                        path=file_record.path,
                        qualified_name=file_record.path,
                        kind="requirements_manifest",
                        start_line=1,
                        end_line=max(1, file_record.line_count),
                        language=file_record.language,
                        analyzer=ANALYZER_VERSION,
                        metadata={},
                    )
                )
                declared: set[str] = set()
                for line_number, line in enumerate(
                    file_sources[file_record.path].splitlines(), start=1
                ):
                    stripped = line.strip()
                    if not stripped or stripped.startswith(("#", "-")):
                        continue
                    match = REQUIREMENT_PATTERN.match(line)
                    if not match:
                        continue
                    dependency = match.group(1).casefold().replace("_", "-")
                    declared.add(dependency)
                    dependency_evidence = receipt(
                        file_record.path,
                        line_number,
                        line_number,
                        "declared_dependency",
                        dependency,
                    )
                    edges.append(
                        EdgeRecord(
                            edge_id=stable_id(
                                "edge",
                                (
                                    snapshot.snapshot_id,
                                    symbol_id,
                                    "declares_dependency",
                                    dependency,
                                    ANALYZER_VERSION,
                                ),
                            ),
                            snapshot_id=snapshot.snapshot_id,
                            source_symbol_id=symbol_id,
                            source_path=file_record.path,
                            relationship="declares_dependency",
                            target_ref=dependency,
                            target_symbol_id=None,
                            evidence_id=dependency_evidence.evidence_id,
                            analyzer=ANALYZER_VERSION,
                        )
                    )
                package_names.update(declared)
                analyzed_files += 1
                continue
            if manifest_name == "pyproject.toml":
                try:
                    project = tomllib.loads(file_sources[file_record.path])
                except tomllib.TOMLDecodeError as exc:
                    failures.append(f"{file_record.path}: {exc.__class__.__name__}: {exc}")
                    continue
                declared_python = _pyproject_dependencies(project)
                manifest_evidence = receipt(
                    file_record.path,
                    1,
                    max(1, file_record.line_count),
                    "project_manifest",
                    file_record.path,
                )
                manifest_receipts.append(manifest_evidence.evidence_id)
                symbol_id = stable_id(
                    "symbol",
                    (
                        snapshot.snapshot_id,
                        file_record.path,
                        "project_manifest",
                        ANALYZER_VERSION,
                    ),
                )
                symbols.append(
                    SymbolRecord(
                        symbol_id=symbol_id,
                        snapshot_id=snapshot.snapshot_id,
                        path=file_record.path,
                        qualified_name=file_record.path,
                        kind="project_manifest",
                        start_line=1,
                        end_line=max(1, file_record.line_count),
                        language=file_record.language,
                        analyzer=ANALYZER_VERSION,
                        metadata={
                            "project_name": _pyproject_name(project),
                            "runtime_dependencies": len(declared_python["runtime"]),
                            "optional_dependencies": len(declared_python["optional"]),
                        },
                    )
                )
                for dependency in sorted(
                    {*declared_python["runtime"], *declared_python["optional"]}
                ):
                    edges.append(
                        EdgeRecord(
                            edge_id=stable_id(
                                "edge",
                                (
                                    snapshot.snapshot_id,
                                    symbol_id,
                                    "declares_dependency",
                                    dependency,
                                    ANALYZER_VERSION,
                                ),
                            ),
                            snapshot_id=snapshot.snapshot_id,
                            source_symbol_id=symbol_id,
                            source_path=file_record.path,
                            relationship="declares_dependency",
                            target_ref=dependency,
                            target_symbol_id=None,
                            evidence_id=manifest_evidence.evidence_id,
                            analyzer=ANALYZER_VERSION,
                        )
                    )
                package_names.update(declared_python["runtime"])
                package_names.update(declared_python["optional"])
                inventory_text = (
                    f"{file_record.path} declares "
                    f"{len(declared_python['runtime'])} runtime and "
                    f"{len(declared_python['optional'])} optional dependencies."
                )
                claims.append(
                    ClaimRecord(
                        claim_id=stable_id(
                            "claim",
                            (
                                snapshot.snapshot_id,
                                "dependency_inventory",
                                inventory_text,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        claim=inventory_text,
                        category="dependency_inventory",
                        status="verified",
                        confidence=1.0,
                        importance="medium",
                        produced_by=ANALYZER_VERSION,
                        created_at=created_at,
                        verified_at=created_at,
                        supporting_evidence=(manifest_evidence.evidence_id,),
                        invalidation_keys=(f"file:{file_record.path}",),
                    )
                )
                analyzed_files += 1
                continue
            if manifest_name != "package.json":
                analyzed_files += 1
                continue
            try:
                document = json.loads(file_sources[file_record.path])
                if not isinstance(document, dict):
                    raise ValueError("top-level package manifest must be an object")
            except (json.JSONDecodeError, ValueError) as exc:
                failures.append(f"{file_record.path}: {exc.__class__.__name__}: {exc}")
                continue

            runtime = document.get("dependencies", {})
            development = document.get("devDependencies", {})
            if not isinstance(runtime, dict) or not isinstance(development, dict):
                failures.append(f"{file_record.path}: dependency fields must be objects")
                continue
            package_names.update(str(name).casefold() for name in runtime)
            package_names.update(str(name).casefold() for name in development)
            manifest_evidence = receipt(
                file_record.path,
                1,
                max(1, file_record.line_count),
                "package_manifest",
                file_record.path,
            )
            manifest_receipts.append(manifest_evidence.evidence_id)
            scripts = document.get("scripts", {})
            if isinstance(scripts, dict) and "test" not in scripts:
                no_test_text = f"{file_record.path} defines no package test script."
                claims.append(
                    ClaimRecord(
                        claim_id=stable_id(
                            "claim",
                            (
                                snapshot.snapshot_id,
                                "testing_gap",
                                no_test_text,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        claim=no_test_text,
                        category="testing_gap",
                        status="verified",
                        confidence=1.0,
                        importance="medium",
                        produced_by=ANALYZER_VERSION,
                        created_at=created_at,
                        verified_at=created_at,
                        supporting_evidence=(manifest_evidence.evidence_id,),
                        invalidation_keys=(f"file:{file_record.path}",),
                    )
                )
            symbol_id = stable_id(
                "symbol",
                (snapshot.snapshot_id, file_record.path, "package_manifest", ANALYZER_VERSION),
            )
            symbols.append(
                SymbolRecord(
                    symbol_id=symbol_id,
                    snapshot_id=snapshot.snapshot_id,
                    path=file_record.path,
                    qualified_name=file_record.path,
                    kind="package_manifest",
                    start_line=1,
                    end_line=max(1, file_record.line_count),
                    language="JSON",
                    analyzer=ANALYZER_VERSION,
                    metadata={
                        "package_name": document.get("name"),
                        "runtime_dependencies": len(runtime),
                        "development_dependencies": len(development),
                    },
                )
            )
            for dependency in sorted({*runtime, *development}):
                edges.append(
                    EdgeRecord(
                        edge_id=stable_id(
                            "edge",
                            (
                                snapshot.snapshot_id,
                                symbol_id,
                                "declares_dependency",
                                dependency,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        source_symbol_id=symbol_id,
                        source_path=file_record.path,
                        relationship="declares_dependency",
                        target_ref=str(dependency),
                        target_symbol_id=None,
                        evidence_id=manifest_evidence.evidence_id,
                        analyzer=ANALYZER_VERSION,
                    )
                )
            claim_text = (
                f"{file_record.path} declares {len(runtime)} runtime and "
                f"{len(development)} development dependencies."
            )
            claims.append(
                ClaimRecord(
                    claim_id=stable_id(
                        "claim",
                        (
                            snapshot.snapshot_id,
                            "dependency_inventory",
                            claim_text,
                            ANALYZER_VERSION,
                        ),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    claim=claim_text,
                    category="dependency_inventory",
                    status="verified",
                    confidence=1.0,
                    importance="medium",
                    produced_by=ANALYZER_VERSION,
                    created_at=created_at,
                    verified_at=created_at,
                    supporting_evidence=(manifest_evidence.evidence_id,),
                    invalidation_keys=(f"file:{file_record.path}",),
                )
            )
            analyzed_files += 1

        tailwind_mentions: list[str] = []
        documented_routes: dict[tuple[str, str], str] = {}
        uvicorn_receipts: list[str] = []
        next_receipts: list[str] = []
        optional_lm_receipts: list[str] = []
        for file_record in eligible:
            if file_record.language != "Markdown" or file_record.path not in file_sources:
                continue
            for line_number, line in enumerate(
                file_sources[file_record.path].splitlines(), start=1
            ):
                route_match = DOCUMENTED_ROUTE_PATTERN.search(line)
                if route_match:
                    route = (route_match.group(1).upper(), route_match.group(2))
                    documented_routes[route] = receipt(
                        file_record.path,
                        line_number,
                        line_number,
                        "documented_http_route",
                        f"{route[0]} {route[1]}",
                    ).evidence_id
                lowered = line.casefold()
                if "uvicorn main:app" in lowered:
                    uvicorn_receipts.append(
                        receipt(
                            file_record.path,
                            line_number,
                            line_number,
                            "documented_runtime_command",
                            "Uvicorn",
                        ).evidence_id
                    )
                if "npm run dev" in lowered:
                    next_receipts.append(
                        receipt(
                            file_record.path,
                            line_number,
                            line_number,
                            "documented_runtime_command",
                            "Next.js",
                        ).evidence_id
                    )
                if "runs fully without lm studio" in lowered:
                    optional_lm_receipts.append(
                        receipt(
                            file_record.path,
                            line_number,
                            line_number,
                            "documented_runtime_boundary",
                            "LM Studio optional",
                        ).evidence_id
                    )
                for match in TAILWIND_PATTERN.finditer(line):
                    prefix = line[max(0, match.start() - 32) : match.start()]
                    if NEGATION_PATTERN.search(prefix):
                        continue
                    tailwind_mentions.append(
                        receipt(
                            file_record.path,
                            line_number,
                            line_number,
                            "documentation_assertion",
                            "Tailwind CSS",
                        ).evidence_id
                    )

        if documented_routes:
            text = (
                f"Markdown API tables document {len(documented_routes)} distinct HTTP "
                "method/path endpoints."
            )
            claims.append(
                ClaimRecord(
                    claim_id=stable_id(
                        "claim",
                        (
                            snapshot.snapshot_id,
                            "documented_http_route_inventory",
                            text,
                            ANALYZER_VERSION,
                        ),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    claim=text,
                    category="documented_http_route_inventory",
                    status="verified",
                    confidence=1.0,
                    importance="medium",
                    produced_by=ANALYZER_VERSION,
                    created_at=created_at,
                    verified_at=created_at,
                    supporting_evidence=tuple(sorted(documented_routes.values())),
                    invalidation_keys=tuple(
                        sorted(
                            f"file:{item.path}"
                            for item in snapshot.files
                            if item.language == "Markdown"
                        )
                    ),
                )
            )

        if uvicorn_receipts and next_receipts and optional_lm_receipts:
            text = (
                "Repository documentation specifies two required application starts (Uvicorn "
                "and Next.js) and states that play runs fully without LM Studio, making LM "
                "Studio a documented optional inference process; runtime was not executed."
            )
            claims.append(
                ClaimRecord(
                    claim_id=stable_id(
                        "claim", (snapshot.snapshot_id, "runtime_topology", text, ANALYZER_VERSION)
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    claim=text,
                    category="runtime_topology",
                    status="inferred",
                    confidence=0.9,
                    importance="high",
                    produced_by=ANALYZER_VERSION,
                    created_at=created_at,
                    supporting_evidence=tuple(
                        sorted({*uvicorn_receipts, *next_receipts, *optional_lm_receipts})
                    ),
                    invalidation_keys=tuple(
                        sorted(
                            f"file:{item.path}"
                            for item in snapshot.files
                            if item.language == "Markdown"
                        )
                    ),
                    alternative_hypotheses=(
                        "The documented launch procedure may be stale or incomplete.",
                    ),
                )
            )

        tailwind_paths = {
            "tailwind.config.js",
            "tailwind.config.cjs",
            "tailwind.config.mjs",
            "tailwind.config.ts",
        }
        has_tailwind_artifact = any(
            Path(item.path).name.casefold() in tailwind_paths for item in snapshot.files
        )
        source_tailwind_signal = any(
            ("@tailwind" in source.casefold() or "tailwindcss" in source.casefold())
            for path, source in file_sources.items()
            if Path(path).suffix.casefold() not in {".md", ".txt"}
        )
        has_tailwind_implementation = (
            "tailwindcss" in package_names or has_tailwind_artifact or source_tailwind_signal
        )

        if tailwind_mentions and not has_tailwind_implementation:
            tailwind_census = receipt(".", None, None, "snapshot_census", "Tailwind CSS")
            text = (
                "Documentation states that Tailwind CSS is used, but this snapshot contains no "
                "Tailwind dependency, configuration artifact, or non-documentation Tailwind marker."
            )
            claims.append(
                ClaimRecord(
                    claim_id=stable_id(
                        "claim",
                        (snapshot.snapshot_id, "documentation_drift", text, ANALYZER_VERSION),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    claim=text,
                    category="documentation_drift",
                    status="conflict",
                    confidence=0.99,
                    importance="high",
                    produced_by=ANALYZER_VERSION,
                    created_at=created_at,
                    supporting_evidence=tuple(sorted(set(tailwind_mentions))),
                    contradicting_evidence=tuple(
                        sorted({*manifest_receipts, tailwind_census.evidence_id})
                    ),
                    invalidation_keys=tuple(
                        sorted(
                            {f"file:{item.path}" for item in snapshot.files} | {"snapshot:file-set"}
                        )
                    ),
                    alternative_hypotheses=(
                        (
                            "Tailwind may be injected outside the repository or only used by an "
                            "untracked build environment."
                        ),
                    ),
                )
            )

        if not any(item.role == "workflow" for item in snapshot.files):
            ci_census = receipt(".", None, None, "snapshot_census", "CI workflows")
            no_ci_text = (
                "The bounded snapshot contains no recognized CI workflow under .github/workflows."
            )
            claims.append(
                ClaimRecord(
                    claim_id=stable_id(
                        "claim",
                        (snapshot.snapshot_id, "delivery_automation", no_ci_text, ANALYZER_VERSION),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    claim=no_ci_text,
                    category="delivery_automation",
                    status="verified",
                    confidence=1.0,
                    importance="medium",
                    produced_by=ANALYZER_VERSION,
                    created_at=created_at,
                    verified_at=created_at,
                    supporting_evidence=(ci_census.evidence_id,),
                    invalidation_keys=("snapshot:file-set",),
                )
            )

        # Stylesheets and markup fetch from third parties too, and neither is
        # any language analyzer's territory. A font `@import` and a background
        # `url()` reach the same third party as a script would, and the census
        # that only read TypeScript reported neither.
        for file_record in snapshot.files:
            if Path(file_record.path).suffix.casefold() not in {".css", ".scss", ".html"}:
                continue
            source = file_sources.get(file_record.path)
            if source is None:
                continue
            seen_here: dict[str, int] = {}
            for index, line in enumerate(source.splitlines(), start=1):
                for match in STYLE_ORIGIN.finditer(line):
                    host = match.group("host").casefold()
                    if host in LOOPBACK_HOSTS or host.endswith(".local"):
                        continue
                    # An XML namespace is an identifier, not a request. Inline
                    # SVG declares one on every element and nothing is fetched.
                    if host in NAMESPACE_HOSTS:
                        continue
                    seen_here.setdefault(host, index)
            for host, line_number in sorted(seen_here.items()):
                origin_receipt = receipt(
                    file_record.path, line_number, line_number, "third_party_origin"
                )
                origin_text = (
                    f"{file_record.path} loads from third-party origin {host}, so every "
                    "visitor's address reaches that host when the page renders."
                )
                claims.append(
                    ClaimRecord(
                        claim_id=stable_id(
                            "claim",
                            (
                                snapshot.snapshot_id,
                                "third_party_origin",
                                origin_text,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        claim=origin_text,
                        category="third_party_origin",
                        status="verified",
                        confidence=1.0,
                        importance="medium",
                        produced_by=ANALYZER_VERSION,
                        created_at=created_at,
                        verified_at=created_at,
                        supporting_evidence=(origin_receipt.evidence_id,),
                        invalidation_keys=(f"file:{file_record.path}",),
                    )
                )

        coverage = CoverageRecord(
            analyzer=ANALYZER_VERSION,
            language="Project metadata and Markdown",
            eligible_files=len(eligible),
            analyzed_files=analyzed_files,
            failed_files=len(failures),
            unsupported_files=0,
            failures=tuple(failures),
        )
        return AnalysisResult(
            snapshot_id=snapshot.snapshot_id,
            analyzer_version=ANALYZER_VERSION,
            created_at=created_at,
            duration_ms=round((time.perf_counter() - started) * 1000),
            symbols=tuple(sorted(symbols, key=lambda item: item.path)),
            edges=tuple(sorted(edges, key=lambda item: item.edge_id)),
            evidence=tuple(sorted(evidence, key=lambda item: item.evidence_id)),
            claims=tuple(sorted(claims, key=lambda item: item.claim_id)),
            coverage=(coverage,),
        )
