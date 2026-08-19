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
    FileRecord,
    Snapshot,
    SymbolRecord,
    utc_now,
)
from open_skeleton.policy import describes_the_product

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


TEXT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{2,}(?:\.[A-Za-z0-9_]{1,6})?")


def _text_name_index(source: str) -> dict[str, int]:
    """Names a non-code file mentions, with the line each first appears on.

    The same concordance the code analyzers build, for the files they do not
    read. A stylesheet names its own keyframes and the assets it loads, a lock
    file names every transitive dependency, and a README names whatever it
    documents — none of which any language analyzer will ever see, and all of
    which someone searching this repository would expect to find.
    """

    found: dict[str, int] = {}
    for number, line in enumerate(source.splitlines(), start=1):
        for match in TEXT_NAME.finditer(line):
            value = match.group(0)
            if len(value) > 60:
                continue
            found[value] = min(found.get(value, number), number)
    return found


DOC_CODE_SPAN = re.compile(r"`([^`\n]{2,80})`")
DOC_IDENTIFIER = re.compile(r"^[A-Za-z_][\w.]*(?:\(\))?$")
DOC_PATH = re.compile(r"^[\w./-]+\.[A-Za-z0-9]{1,5}$")
DOC_NUMBER = re.compile(r"\b(\d[\d,]{0,8}(?:\.\d+)?)\b")
# A bare common word in backticks is formatting, not an assertion about code.
DOC_NOISE = frozenset(
    {"true", "false", "none", "null", "get", "post", "put", "delete", "patch", "self", "the"}
)


def _documented_facts(source: str) -> dict[str, dict[str, Any]]:
    """Identifiers and values a document asserts about the code beside it.

    Documentation is the only artifact in a repository that states intent, and
    it is the one that goes stale silently: nothing fails when a README keeps
    claiming a limit the code stopped using. Recovering what it asserts is what
    makes the disagreement visible later.

    A number is attributed to an identifier only when it appears close to it,
    which is a proximity heuristic and is why the result is reported as what
    the document says rather than as what is true.
    """

    found: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(source.splitlines(), start=1):
        for match in DOC_CODE_SPAN.finditer(line):
            raw = match.group(1).split(":")[0].strip()
            if not (DOC_IDENTIFIER.match(raw) or DOC_PATH.match(raw)):
                continue
            name = raw.rstrip("()")
            lowered = name.casefold()
            if lowered in DOC_NOISE or ("." not in name and len(name) < 4):
                continue
            entry = found.setdefault(name, {"line": number, "values": []})
            entry["line"] = min(int(entry["line"]), number)
            nearby = line[match.end() : match.end() + 90]
            for value in DOC_NUMBER.findall(nearby):
                cleaned = value.replace(",", "")
                if cleaned not in entry["values"] and len(cleaned) <= 9:
                    entry["values"].append(cleaned)
    return found


# A custom property is declared at the start of a declaration block or after a
# semicolon. Requiring one of those anchors keeps `var(--x)` uses out of the
# declaration set, which is the whole difference between what a stylesheet
# defines and what it merely mentions.
CSS_CUSTOM_PROPERTY = re.compile(r"(?:^|[;{])\s*(--[\w-]+)\s*:\s*([^;{}]{0,80})", re.M)


def _declared_design_tokens(source: str) -> dict[str, dict[str, Any]]:
    """Custom properties a stylesheet declares, with value and line.

    Design tokens are what a web interface is actually built from -- every
    colour, spacing step and font stack behind a name -- and changing one
    changes every rule that reads it. They are declared in every stylesheet in
    this corpus without exception, and no reader touched them: a stylesheet
    reached only the name index, which records that `--bg` occurs somewhere
    and not that it is defined here or what it is set to.

    Only declarations are collected, never `var(--x)` uses. Whether a token is
    used, or declared elsewhere, is deliberately not asserted: a token can be
    set by a framework font loader or by a React inline style object, both of
    which are invisible to a stylesheet reader, and calling those undeclared
    would be a confident falsehood about working code.
    """

    found: dict[str, dict[str, Any]] = {}
    line = 1
    position = 0
    for match in CSS_CUSTOM_PROPERTY.finditer(source):
        # The line of the name, not of the match: the pattern begins at the
        # `{` or `;` anchoring the declaration, and that is usually on the
        # line above the token it introduces.
        line += source.count("\n", position, match.start(1))
        position = match.start(1)
        name = match.group(1)
        value = " ".join(match.group(2).split())
        if name not in found:
            found[name] = {"value": value, "line": line}
    return found


HTML_SCRIPT = re.compile(r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
HTML_STYLESHEET = re.compile(
    r"<link\b(?=[^>]*\brel\s*=\s*[\"']stylesheet[\"'])[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
# A scheme or a protocol-relative prefix means a third party serves it, which
# the egress reader already reports. Only same-repository files are a module
# graph.
ABSOLUTE_REFERENCE = re.compile(r"^(?:[a-zA-Z][\w+.-]*:|//)")


def _referenced_assets(source: str) -> list[tuple[str, str, int]]:
    """Scripts and stylesheets a document loads, in the order it lists them.

    For an application with no bundler, the HTML document *is* the module
    graph: it names every script, and the order it names them in is the order
    they execute. Nothing else in such a repository states that order, and
    this engine read the document only for its names -- recording that
    `style.css` is mentioned somewhere, not that it is loaded here and first.

    A reference with a scheme is left out. Those are served by somebody else
    and are already reported as third-party origins; treating them as local
    modules would put a CDN in the module graph.
    """

    found: list[tuple[str, str, int]] = []
    for kind, pattern in (("script", HTML_SCRIPT), ("stylesheet", HTML_STYLESHEET)):
        for match in pattern.finditer(source):
            reference = match.group(1).strip()
            if not reference or ABSOLUTE_REFERENCE.match(reference):
                continue
            line = source.count("\n", 0, match.start()) + 1
            found.append((kind, reference.split("?")[0].split("#")[0], line))
    found.sort(key=lambda item: item[2])
    return found


def _strip_json_comments(source: str) -> str:
    """Remove the comments a tsconfig is allowed to carry but JSON is not.

    tsconfig.json is JSONC by convention and the files in the wild use it, so a
    strict parser rejects perfectly ordinary configurations. Strings are
    tracked so a `//` inside one is left alone.
    """

    out: list[str] = []
    index = 0
    length = len(source)
    in_string = False
    while index < length:
        character = source[index]
        if in_string:
            out.append(character)
            if character == "\\" and index + 1 < length:
                out.append(source[index + 1])
                index += 2
                continue
            if character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            out.append(character)
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index)
            index = length if newline < 0 else newline
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        out.append(character)
        index += 1

    # Trailing commas are the other thing JSONC tolerates, and they have to be
    # removed after the comments rather than alongside them: a comment sitting
    # between the comma and its closing brace hides the brace from a
    # single-pass scan, which is exactly how real configurations are written.
    stripped = "".join(out)
    cleaned: list[str] = []
    index = 0
    length = len(stripped)
    in_string = False
    while index < length:
        character = stripped[index]
        if in_string:
            cleaned.append(character)
            if character == "\\" and index + 1 < length:
                cleaned.append(stripped[index + 1])
                index += 2
                continue
            if character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
        elif character == "," and stripped[index + 1 :].lstrip()[:1] in {"}", "]"}:
            index += 1
            continue
        cleaned.append(character)
        index += 1
    return "".join(cleaned)


def _flatten_settings(document: Any, prefix: str = "") -> dict[str, str]:
    """Scalar settings as dotted keys, so a nested option is still searchable."""

    flat: dict[str, str] = {}
    if isinstance(document, dict):
        for key, value in document.items():
            flat.update(_flatten_settings(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(document, list):
        rendered = ", ".join(str(item) for item in document if isinstance(item, (str, int, float)))
        if rendered and prefix:
            flat[prefix] = rendered
    elif isinstance(document, bool):
        flat[prefix] = "true" if document else "false"
    elif isinstance(document, (str, int, float)) and prefix:
        flat[prefix] = str(document)
    return flat


def _normalize_requirement(value: object) -> str | None:
    """Reduce a PEP 508 requirement string to its normalized distribution name."""

    if not isinstance(value, str):
        return None
    match = REQUIREMENT_PATTERN.match(value)
    if not match:
        return None
    return match.group(1).casefold().replace("_", "-")


def _declared_license(document: dict[str, Any], manifest: str) -> str | None:
    """The licence identifier a manifest states, if it states one.

    Under what terms a repository may be used is among the first questions
    asked of it, and this engine answered only that a file named `LICENSE`
    exists -- never what it says. Every ecosystem carries the identifier in
    its manifest, so the answer was already being parsed and thrown away.

    Returns the identifier as written. No attempt is made to normalise
    `Apache-2.0` against `Apache License 2.0`: the value is quoted, not
    interpreted, and a reader comparing two spellings is better served by
    seeing both than by a guess about which was meant.
    """

    table: object
    if manifest == "Cargo.toml":
        # A Cargo workspace declares the licence once, under `[workspace.package]`,
        # and every member writes `license.workspace = true` to inherit it. The
        # inheritance marker is not an identifier and is not treated as one:
        # resolving it needs the root manifest, which this file is not, so the
        # claim is emitted where the value is actually written.
        package = document.get("package")
        table = package.get("license") if isinstance(package, dict) else None
        if not isinstance(table, str):
            workspace = document.get("workspace")
            shared = workspace.get("package") if isinstance(workspace, dict) else None
            table = shared.get("license") if isinstance(shared, dict) else None
    elif manifest == "pyproject.toml":
        project = document.get("project")
        table = project.get("license") if isinstance(project, dict) else None
    else:
        # npm's legacy spelling is a separate key holding a list, not the same
        # key holding a different type, so looking only at `license` finds
        # nothing in a package that uses it.
        table = document.get("license")
        if table is None:
            table = document.get("licenses")
    if isinstance(table, str):
        return table.strip() or None
    # PEP 621 allowed a table with `text`; npm's legacy form is a list.
    if isinstance(table, dict):
        text = table.get("text")
        return str(text).strip() or None if isinstance(text, str) else None
    if isinstance(table, list) and table:
        first = table[0]
        if isinstance(first, dict) and isinstance(first.get("type"), str):
            return str(first["type"]).strip() or None
    return None


def _declared_commands(document: dict[str, Any], manifest: str) -> list[tuple[str, str]]:
    """Commands a manifest installs, as ``(name, target)`` pairs.

    A `__main__` guard says a file can be run. It does not say what a user
    types, and the name they type is declared here rather than in the code:
    `open-skeleton = "open_skeleton.cli:main"` is the difference between "this
    module is runnable" and "this package installs a command called
    open-skeleton". The engine reported the first and never the second.

    Three ecosystems spell it differently and all three are read, because a
    rule that only understood Python would report a Node package as shipping
    no commands at all -- which is a statement about this reader.
    """

    found: list[tuple[str, str]] = []
    if manifest == "pyproject.toml":
        project = document.get("project")
        if isinstance(project, dict):
            for table in ("scripts", "gui-scripts"):
                entries = project.get(table)
                if isinstance(entries, dict):
                    found.extend(
                        (str(name), str(target))
                        for name, target in entries.items()
                        if isinstance(target, str)
                    )
    elif manifest == "package.json":
        binaries = document.get("bin")
        if isinstance(binaries, str):
            name = document.get("name")
            if isinstance(name, str):
                found.append((name, binaries))
        elif isinstance(binaries, dict):
            found.extend(
                (str(name), str(target))
                for name, target in binaries.items()
                if isinstance(target, str)
            )
    elif manifest == "Cargo.toml":
        binaries = document.get("bin")
        if isinstance(binaries, list):
            for entry in binaries:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    found.append((str(entry["name"]), str(entry.get("path", "src/main.rs"))))
    return sorted(dict.fromkeys(found))


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


# Documents whose filename declares that their contents are commitments rather
# than description. A README lists what a thing does; a requirements or gates
# document lists what it must do, and the difference is what makes one worth
# recording as an obligation.
DECLARATIVE_DOCUMENT_MARKERS = (
    "requirement",
    "gate",
    "roadmap",
    "threat",
    "security",
    "contributing",
    "agents",
    "milestone",
    "charter",
    "principles",
    "governance",
    "completion",
    "acceptance",
    "sla",
    # An architecture decision record states a decision and its consequences,
    # which is a commitment in the same sense. They are conventionally filed
    # under a `decisions/` or `adr/` directory and numbered.
    "decision",
    "adr",
)
# A heading that names what a project has decided *not* to do. The bullets
# under it are declared absences, not obligations, and calling six things a
# project refuses to build "stated obligations" inverts their meaning.
NON_GOAL_MARKERS = (
    "non-goal",
    "nongoal",
    "out of scope",
    "not in scope",
    "will not",
    "deliberately does not",
    "does not do",
    "excluded",
)
# One bullet under a heading is a remark. Several are a list of obligations.
MIN_OBLIGATIONS = 2


def is_non_goal_heading(heading: str) -> bool:
    """Whether a heading names declared absences rather than obligations."""

    folded = heading.casefold()
    return any(marker in folded for marker in NON_GOAL_MARKERS)


def _cargo_name(document: dict[str, Any]) -> str | None:
    package = document.get("package")
    if isinstance(package, dict) and isinstance(package.get("name"), str):
        return str(package["name"])
    return None


def _cargo_dependencies(document: dict[str, Any]) -> dict[str, set[str]]:
    """Collect Cargo runtime and development dependency names.

    A Cargo dependency is a key whose value is either a version string or a
    table. Both spellings name the same crate, so the key is what matters --
    except when the table carries `package`, which renames the crate: under
    `serde_json = { package = "sonic-rs" }` the crate fetched is `sonic-rs`
    and reporting the alias would name a package that is never downloaded.

    `dev-dependencies` and `build-dependencies` are grouped as optional
    because neither is present in a consumer's build, which is the
    distinction a reader of the inventory is drawing.

    A dependency carrying `path` is a sibling crate in this same repository,
    not something fetched from a registry, and it is reported separately:
    counting a workspace's own crates as third-party supply chain would
    inflate the number that a reader asking "what does this repository pull
    in" is actually asking about.
    """

    runtime: set[str] = set()
    optional: set[str] = set()
    internal: set[str] = set()

    def collect(table: object, into: set[str]) -> None:
        if not isinstance(table, dict):
            return
        for name, value in table.items():
            crate = str(name)
            local = False
            if isinstance(value, dict):
                if isinstance(value.get("package"), str):
                    crate = str(value["package"])
                local = isinstance(value.get("path"), str)
            normalized = crate.casefold().replace("_", "-")
            if normalized:
                (internal if local else into).add(normalized)

    collect(document.get("dependencies"), runtime)
    collect(document.get("dev-dependencies"), optional)
    collect(document.get("build-dependencies"), optional)
    workspace = document.get("workspace")
    if isinstance(workspace, dict):
        collect(workspace.get("dependencies"), runtime)
    return {"runtime": runtime, "optional": optional, "internal": internal}


def _checked_out_revision(root: Path) -> tuple[str, str] | None:
    """`(revision, ref)` for the commit checked out, or None outside a git tree.

    A snapshot identifies the bytes on disk when it was taken, which is a
    deterministic function of a directory and not of a commit. Two people on
    the same revision can hold different working trees, so a reader with only
    a snapshot identifier cannot tell which revision it corresponds to, and a
    diff between two machines would read as change rather than as difference.

    Recording the revision does not make the snapshot commit-addressed, and
    the claim says so: the tree may carry edits this does not describe. It
    turns "some state of this repository" into "this revision, plus whatever
    was uncommitted", which is the honest version of the same fact.

    `.git` is excluded from analysis, and reading it here is provenance rather
    than analysis: no target code runs and nothing is written.
    """

    head = root / ".git" / "HEAD"
    if not head.is_file():
        return None
    try:
        contents = head.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not contents.startswith("ref: "):
        return (contents, "detached HEAD") if len(contents) >= 7 else None
    ref = contents.removeprefix("ref: ").strip()
    direct = root / ".git" / Path(ref)
    if direct.is_file():
        try:
            return direct.read_text(encoding="utf-8", errors="replace").strip(), ref
        except OSError:
            return None
    packed = root / ".git" / "packed-refs"
    if packed.is_file():
        try:
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.endswith(f" {ref}"):
                    return line.split(" ", 1)[0].strip(), ref
        except OSError:
            return None
    return None


def is_declarative_document(path: str) -> bool:
    """Whether a document's name says its contents are commitments."""

    folded = path.casefold()
    stem = folded.rsplit("/", 1)[-1]
    directories = folded.rsplit("/", 1)[0] if "/" in folded else ""
    return any(
        marker in stem or f"/{marker}" in f"/{directories}"
        for marker in DECLARATIVE_DOCUMENT_MARKERS
    )


def _declared_commitments(source: str) -> list[tuple[str, int, int]]:
    """`(heading, obligation count, line)` for each commitment group in a document.

    A specification generator can extract what a codebase *does* from the code.
    What it cannot recover that way is what the codebase said it *must* do, and
    that lives in prose: a heading naming an obligation group, followed by the
    bullets that spell it out. `## G1: Safe repository boundary` and the three
    lines under it are a commitment; the same shape appears in a requirements
    document, a threat model, and a contributing guide.

    Nothing here checks whether an obligation holds. Recording that the
    repository made the promise is a different fact from whether it kept it,
    and only the first is visible in a document.
    """

    found: list[tuple[str, int, int]] = []
    heading = ""
    heading_line = 0
    obligations = 0
    fenced = False
    for number, line in enumerate(source.splitlines(), start=1):
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.startswith("#"):
            if heading and obligations >= MIN_OBLIGATIONS:
                found.append((heading, obligations, heading_line))
            heading = line.lstrip("#").strip()
            heading_line = number
            obligations = 0
            continue
        stripped = line.strip()
        if stripped.startswith(("- ", "* ", "+ ")) or re.match(r"^\d+\. ", stripped):
            obligations += 1
    if heading and obligations >= MIN_OBLIGATIONS:
        found.append((heading, obligations, heading_line))
    return found


class ProjectMetadataAnalyzer:
    name = ANALYZER_NAME
    version = ANALYZER_VERSION
    eligibility = "language"

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
            in {"package.json", "requirements.txt", "pyproject.toml", "cargo.toml"}
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

        def declare_license(
            document: dict[str, Any],
            manifest: str,
            file_record: FileRecord,
            manifest_evidence: EvidenceRecord,
        ) -> None:
            """Emit the licence identifier a manifest states, if it states one.

            Every ecosystem spells this differently and every reader of any
            repository wants it, so the claim is emitted from each manifest
            branch rather than from the one that happened to be written first.
            """

            licence = _declared_license(document, manifest)
            if not licence:
                return
            licence_text = (
                f"`{file_record.path}` declares the repository licence as "
                f"`{licence}`. This is the identifier the manifest states; the terms "
                "themselves are in whatever file it points at."
            )
            claims.append(
                ClaimRecord(
                    claim_id=stable_id(
                        "claim",
                        (
                            snapshot.snapshot_id,
                            "declared_license",
                            licence_text,
                            ANALYZER_VERSION,
                        ),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    claim=licence_text,
                    category="declared_license",
                    status="verified",
                    confidence=1.0,
                    importance="high",
                    produced_by=ANALYZER_VERSION,
                    created_at=created_at,
                    verified_at=created_at,
                    supporting_evidence=(manifest_evidence.evidence_id,),
                    invalidation_keys=(f"file:{file_record.path}",),
                )
            )

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
            if manifest_name == "cargo.toml":
                try:
                    crate = tomllib.loads(file_sources[file_record.path])
                except tomllib.TOMLDecodeError as exc:
                    failures.append(f"{file_record.path}: {exc.__class__.__name__}: {exc}")
                    continue
                declared_rust = _cargo_dependencies(crate)
                manifest_evidence = receipt(
                    file_record.path,
                    1,
                    max(1, file_record.line_count),
                    "project_manifest",
                    file_record.path,
                )
                manifest_receipts.append(manifest_evidence.evidence_id)
                declare_license(crate, "Cargo.toml", file_record, manifest_evidence)
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
                            "project_name": _cargo_name(crate),
                            "runtime_dependencies": len(declared_rust["runtime"]),
                            "optional_dependencies": len(declared_rust["optional"]),
                            "internal_dependencies": len(declared_rust["internal"]),
                        },
                    )
                )
                for dependency in sorted(
                    {
                        *declared_rust["runtime"],
                        *declared_rust["optional"],
                        *declared_rust["internal"],
                    }
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
                package_names.update(declared_rust["runtime"])
                package_names.update(declared_rust["optional"])
                package_names.update(declared_rust["internal"])
                if any(declared_rust.values()):
                    internal_note = (
                        f" A further {len(declared_rust['internal'])} are declared by path "
                        "and live in this repository rather than a registry."
                        if declared_rust["internal"]
                        else ""
                    )
                    inventory_text = (
                        f"{file_record.path} declares "
                        f"{len(declared_rust['runtime'])} runtime and "
                        f"{len(declared_rust['optional'])} optional dependencies."
                        f"{internal_note}"
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
                commands = _declared_commands(project, "pyproject.toml")
                declare_license(project, "pyproject.toml", file_record, manifest_evidence)
                if commands:
                    named = ", ".join(f"`{name}` (`{target}`)" for name, target in commands)
                    command_text = (
                        f"`{file_record.path}` installs {len(commands):,} command(s): "
                        f"{named}. These are the names a user types; a `__main__` guard "
                        "elsewhere says only that a module can be run."
                    )
                    claims.append(
                        ClaimRecord(
                            claim_id=stable_id(
                                "claim",
                                (
                                    snapshot.snapshot_id,
                                    "application_entry",
                                    command_text,
                                    ANALYZER_VERSION,
                                ),
                            ),
                            snapshot_id=snapshot.snapshot_id,
                            claim=command_text,
                            category="application_entry",
                            status="verified",
                            confidence=1.0,
                            importance="high",
                            produced_by=ANALYZER_VERSION,
                            created_at=created_at,
                            verified_at=created_at,
                            supporting_evidence=(manifest_evidence.evidence_id,),
                            invalidation_keys=(f"file:{file_record.path}",),
                        )
                    )
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
            if is_declarative_document(file_record.path):
                for heading, obligations, heading_line in _declared_commitments(
                    file_sources[file_record.path]
                ):
                    commitment_receipt = receipt(
                        file_record.path,
                        heading_line,
                        heading_line,
                        "declared_commitment",
                        heading,
                    )
                    non_goal = is_non_goal_heading(heading)
                    commitment_text = (
                        f"{file_record.path} places {obligations:,} thing(s) outside this "
                        f'project under "{heading}". A concern named here is absent by '
                        "decision rather than by omission, and this claim records the "
                        "decision, not whether the code honours it."
                        if non_goal
                        else f'{file_record.path} declares "{heading}" with {obligations:,} '
                        "stated obligation(s). This records that the commitment was made; "
                        "whether the code keeps it is a separate question this claim does "
                        "not answer."
                    )
                    claims.append(
                        ClaimRecord(
                            claim_id=stable_id(
                                "claim",
                                (
                                    snapshot.snapshot_id,
                                    "declared_non_goal" if non_goal else "declared_commitment",
                                    commitment_text,
                                    ANALYZER_VERSION,
                                ),
                            ),
                            snapshot_id=snapshot.snapshot_id,
                            claim=commitment_text,
                            category="declared_non_goal" if non_goal else "declared_commitment",
                            status="verified",
                            confidence=1.0,
                            importance="medium",
                            produced_by=ANALYZER_VERSION,
                            created_at=created_at,
                            verified_at=created_at,
                            supporting_evidence=(commitment_receipt.evidence_id,),
                            invalidation_keys=(f"file:{file_record.path}",),
                        )
                    )
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

        revision = _checked_out_revision(snapshot.root)
        if revision is not None:
            commit, ref = revision
            revision_receipt = receipt(".", None, None, "snapshot_census", "checked-out revision")
            revision_text = (
                f"The working tree was checked out at {commit[:12]} on {ref} when this "
                "snapshot was taken. The snapshot identifies the bytes on disk, not that "
                "commit, so uncommitted edits are included and are not distinguished here."
            )
            claims.append(
                ClaimRecord(
                    claim_id=stable_id(
                        "claim",
                        (snapshot.snapshot_id, "checked_out_revision", commit, ANALYZER_VERSION),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    claim=revision_text,
                    category="checked_out_revision",
                    status="verified",
                    confidence=1.0,
                    # Provenance rather than a finding. It belongs in the
                    # composition section a reader checks before trusting the
                    # rest, not at the top of a list headed "highest-importance
                    # verified findings", where it crowded out the findings.
                    importance="medium",
                    produced_by=ANALYZER_VERSION,
                    created_at=created_at,
                    verified_at=created_at,
                    supporting_evidence=(revision_receipt.evidence_id,),
                    invalidation_keys=("git:HEAD",),
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

        # Files no language analyzer reads still name things: a stylesheet
        # names its keyframes and assets, a lock file names every transitive
        # dependency, a README names whatever it documents.
        for file_record in snapshot.files:
            name = Path(file_record.path).name.casefold()
            suffix = Path(file_record.path).suffix.casefold()
            # `.webmanifest` is a web app's own description of itself -- name,
            # start URL, theme -- and was Unknown to every reader here. A
            # `.gitignore` names the directories a project generates, which is
            # now load-bearing: the scanner decides the census from it, so the
            # patterns it used should be searchable rather than only inferable
            # from the exclusion rows.
            indexed_name = name in {".gitignore", ".dockerignore", ".npmrc", ".editorconfig"}
            if not indexed_name and suffix not in {
                ".css",
                ".scss",
                ".json",
                ".md",
                ".txt",
                ".toml",
                ".html",
                ".webmanifest",
                ".yaml",
                ".yml",
            }:
                continue
            source = file_sources.get(file_record.path)
            if source is None:
                continue
            names = _text_name_index(source)
            if suffix in {".html", ".htm"} and describes_the_product(file_record.role):
                assets = _referenced_assets(source)
                if assets:
                    asset_evidence = receipt(
                        file_record.path,
                        assets[0][2],
                        assets[-1][2],
                        "referenced_assets",
                        file_record.path,
                    )
                    scripts = [item for item in assets if item[0] == "script"]
                    sheets = [item for item in assets if item[0] == "stylesheet"]
                    ordered = ", ".join(f"`{reference}`" for _, reference, _ in scripts[:8])
                    more = f" and {len(scripts) - 8:,} more" if len(scripts) > 8 else ""
                    asset_text = (
                        f"`{file_record.path}` loads {len(scripts):,} script(s) and "
                        f"{len(sheets):,} stylesheet(s) from this repository"
                        + (f", in order: {ordered}{more}" if scripts else "")
                        + ". For an application with no bundler this document is the "
                        "module graph, and nothing else states the order."
                    )
                    claims.append(
                        ClaimRecord(
                            claim_id=stable_id(
                                "claim",
                                (
                                    snapshot.snapshot_id,
                                    "application_entry",
                                    asset_text,
                                    ANALYZER_VERSION,
                                ),
                            ),
                            snapshot_id=snapshot.snapshot_id,
                            claim=asset_text,
                            category="application_entry",
                            status="verified",
                            confidence=1.0,
                            importance="high",
                            produced_by=ANALYZER_VERSION,
                            created_at=created_at,
                            verified_at=created_at,
                            supporting_evidence=(asset_evidence.evidence_id,),
                            invalidation_keys=(f"file:{file_record.path}",),
                        )
                    )
                    document_symbol = stable_id(
                        "symbol",
                        (snapshot.snapshot_id, file_record.path, "document", ANALYZER_VERSION),
                    )
                    symbols.append(
                        SymbolRecord(
                            symbol_id=document_symbol,
                            snapshot_id=snapshot.snapshot_id,
                            path=file_record.path,
                            qualified_name=file_record.path,
                            kind="document",
                            start_line=1,
                            end_line=max(1, file_record.line_count),
                            language=file_record.language,
                            analyzer=ANALYZER_VERSION,
                            metadata={"loads": [reference for _, reference, _ in assets]},
                        )
                    )
                    for _, reference, asset_line in assets:
                        edges.append(
                            EdgeRecord(
                                edge_id=stable_id(
                                    "edge",
                                    (
                                        snapshot.snapshot_id,
                                        document_symbol,
                                        "loads",
                                        reference,
                                        ANALYZER_VERSION,
                                    ),
                                ),
                                snapshot_id=snapshot.snapshot_id,
                                source_symbol_id=document_symbol,
                                source_path=file_record.path,
                                relationship="loads",
                                target_ref=reference,
                                target_symbol_id=None,
                                evidence_id=asset_evidence.evidence_id,
                                analyzer=ANALYZER_VERSION,
                            )
                        )
                        names.setdefault(reference, asset_line)
            if suffix in {".css", ".scss"}:
                tokens = _declared_design_tokens(source)
                if tokens:
                    token_evidence = receipt(
                        file_record.path,
                        min(int(entry["line"]) for entry in tokens.values()),
                        max(int(entry["line"]) for entry in tokens.values()),
                        "design_tokens",
                        file_record.path,
                    )
                    named = ", ".join(f"`{name}`" for name in sorted(tokens)[:10])
                    more = f" and {len(tokens) - 10:,} more" if len(tokens) > 10 else ""
                    token_text = (
                        f"`{file_record.path}` declares {len(tokens):,} design token(s): "
                        f"{named}{more}. These are the named values the interface is built "
                        "from; changing one changes every rule that reads it."
                    )
                    claims.append(
                        ClaimRecord(
                            claim_id=stable_id(
                                "claim",
                                (
                                    snapshot.snapshot_id,
                                    "design_tokens",
                                    token_text,
                                    ANALYZER_VERSION,
                                ),
                            ),
                            snapshot_id=snapshot.snapshot_id,
                            claim=token_text,
                            category="design_tokens",
                            status="verified",
                            confidence=1.0,
                            importance="medium",
                            produced_by=ANALYZER_VERSION,
                            created_at=created_at,
                            verified_at=created_at,
                            supporting_evidence=(token_evidence.evidence_id,),
                            invalidation_keys=(f"file:{file_record.path}",),
                        )
                    )
                    for name, entry in tokens.items():
                        names.setdefault(name, int(entry["line"]))
            if not names:
                continue
            symbols.append(
                SymbolRecord(
                    symbol_id=stable_id(
                        "symbol",
                        (snapshot.snapshot_id, file_record.path, "text_names", ANALYZER_VERSION),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    path=file_record.path,
                    qualified_name=file_record.path,
                    kind="text_names",
                    start_line=1,
                    end_line=max(1, file_record.line_count),
                    language=file_record.language,
                    analyzer=ANALYZER_VERSION,
                    metadata={"name_index": names},
                )
            )

        # Documentation is the only artifact that states intent, and the one
        # that goes stale silently: nothing fails when a README keeps claiming
        # a limit the code stopped using. Recording what it asserts is what
        # lets the disagreement be shown later.
        for file_record in snapshot.files:
            if file_record.language != "Markdown":
                continue
            source = file_sources.get(file_record.path)
            if source is None:
                continue
            asserted = _documented_facts(source)
            if not asserted:
                continue
            symbols.append(
                SymbolRecord(
                    symbol_id=stable_id(
                        "symbol",
                        (
                            snapshot.snapshot_id,
                            file_record.path,
                            "documentation",
                            ANALYZER_VERSION,
                        ),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    path=file_record.path,
                    qualified_name=file_record.path,
                    kind="documentation",
                    start_line=1,
                    end_line=max(1, file_record.line_count),
                    language="Markdown",
                    analyzer=ANALYZER_VERSION,
                    metadata={"documented_facts": asserted},
                )
            )

        # Build and compiler settings decide what the toolchain will accept.
        # `"strict": false` in a tsconfig is a fact about how much of this
        # codebase is actually type-checked, and it lived in a file no analyzer
        # opened.
        for file_record in snapshot.files:
            name = Path(file_record.path).name.casefold()
            if not (name.startswith(("tsconfig", "jsconfig")) and name.endswith(".json")):
                continue
            source = file_sources.get(file_record.path)
            if source is None:
                continue
            try:
                document = json.loads(_strip_json_comments(source))
            except (json.JSONDecodeError, ValueError):
                failures.append(f"{file_record.path}: unparsed compiler configuration")
                continue
            settings = _flatten_settings(document)
            if not settings:
                continue
            config_evidence = receipt(file_record.path, 1, None, "compiler_configuration")
            symbols.append(
                SymbolRecord(
                    symbol_id=stable_id(
                        "symbol",
                        (
                            snapshot.snapshot_id,
                            file_record.path,
                            "compiler_configuration",
                            ANALYZER_VERSION,
                        ),
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    path=file_record.path,
                    qualified_name=file_record.path,
                    kind="compiler_configuration",
                    start_line=1,
                    end_line=max(1, file_record.line_count),
                    language="JSON",
                    analyzer=ANALYZER_VERSION,
                    metadata={"config_settings": settings},
                )
            )
            strictness = settings.get("compilerOptions.strict")
            if strictness is not None:
                strict_text = (
                    f"{file_record.path} sets compilerOptions.strict to {strictness}, which "
                    "decides how much of this codebase the type checker actually checks."
                )
                claims.append(
                    ClaimRecord(
                        claim_id=stable_id(
                            "claim",
                            (
                                snapshot.snapshot_id,
                                "compiler_configuration",
                                strict_text,
                                ANALYZER_VERSION,
                            ),
                        ),
                        snapshot_id=snapshot.snapshot_id,
                        claim=strict_text,
                        category="compiler_configuration",
                        status="verified",
                        confidence=1.0,
                        importance="medium",
                        produced_by=ANALYZER_VERSION,
                        created_at=created_at,
                        verified_at=created_at,
                        supporting_evidence=(config_evidence.evidence_id,),
                        invalidation_keys=(f"file:{file_record.path}",),
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
