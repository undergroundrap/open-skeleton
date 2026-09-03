# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

POLICY_VERSION = "inventory-v1"


EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".open-skeleton",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".next",
        ".nuxt",
        ".cache",
        "htmlcov",
    }
)
# Names that usually hold generated output and sometimes hold source. `build/`
# is the clearest case: two repositories in this corpus keep a hand-written
# `build/sites-vite-plugin.ts` there -- build *tooling*, not build output --
# and a fixed list deleted it from the census for the name of its parent.
#
# So these are not decided by name. A repository states which of its
# directories are generated, in its own `.gitignore`, and that statement is
# read instead. The list below applies only to a repository that states
# nothing at all, where there is no evidence to prefer over a guess.
GENERATED_DIRECTORY_NAMES = frozenset(
    {
        "build",
        "coverage",
        "dist",
        "target",
        "vendor",
    }
)
# Build output whose directory name carries the package name, so no fixed
# set can match it. `src/open_skeleton.egg-info/` was being read as source:
# six generated files, none of them tracked by git, counted in the file
# census and available to every analyzer as if a person had written them.
EXCLUDED_DIRECTORY_SUFFIXES = (".egg-info", ".dist-info", ".egg")

SENSITIVE_FILE_NAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "credentials",
        "credentials.json",
        "secrets.json",
        "id_rsa",
        "id_ed25519",
    }
)

SENSITIVE_SUFFIXES = frozenset(
    {
        ".key",
        ".pem",
        ".p12",
        ".pfx",
        ".jks",
        ".keystore",
    }
)

BINARY_SUFFIXES = frozenset(
    {
        ".7z",
        ".a",
        ".avi",
        ".bin",
        ".bmp",
        ".class",
        ".db",
        ".dll",
        ".dylib",
        ".eot",
        ".exe",
        ".gif",
        ".gz",
        ".ico",
        ".jar",
        ".jpeg",
        ".jpg",
        ".lockb",
        ".mp3",
        ".mp4",
        ".o",
        ".otf",
        ".pdf",
        ".png",
        ".pyc",
        ".sqlite",
        ".sqlite3",
        ".so",
        ".tar",
        ".ttf",
        ".wav",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
        ".xz",
        ".zip",
    }
)

LANGUAGES_BY_SUFFIX = {
    # An installer or a deployment step is often the only place a project
    # writes down how it is actually run, and PowerShell was Unknown --
    # on a Windows-first project whose own README is PowerShell, and whose
    # sibling repository ships `install.ps1` as its entry point. Batch and
    # the shell variants were missing for the same reason: nobody had a
    # file of that kind in front of them at the time.
    # `.mjs` and `.cjs` are how Node spells an ES module and a CommonJS one,
    # and `.mts`/`.cts` are their TypeScript counterparts. Omitting them made
    # every such file Unknown, so no analyzer read it: billune's entire test
    # suite is one `.mjs` file, and the specification reported all seven of
    # its capabilities as reached by no test while the suite sat in `tests/`
    # with the `test` role already assigned to it.
    ".bash": "Shell",
    ".bat": "Batch",
    ".c": "C",
    ".cc": "C++",
    ".cjs": "JavaScript",
    ".cpp": "C++",
    ".cmd": "Batch",
    ".cs": "C#",
    ".cts": "TypeScript",
    ".css": "CSS",
    ".go": "Go",
    ".h": "C/C++ Header",
    ".hpp": "C++ Header",
    ".html": "HTML",
    ".hum": "Hum",
    ".java": "Java",
    ".js": "JavaScript",
    ".json": "JSON",
    ".jsx": "JavaScript JSX",
    ".kt": "Kotlin",
    ".lua": "Lua",
    ".md": "Markdown",
    ".mjs": "JavaScript",
    ".mts": "TypeScript",
    ".php": "PHP",
    ".ps1": "PowerShell",
    ".psd1": "PowerShell",
    ".psm1": "PowerShell",
    ".proto": "Protocol Buffers",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".scss": "SCSS",
    ".sh": "Shell",
    ".sql": "SQL",
    ".swift": "Swift",
    ".toml": "TOML",
    ".ts": "TypeScript",
    ".tsx": "TypeScript JSX",
    ".txt": "Text",
    ".xml": "XML",
    ".zsh": "Shell",
    ".yaml": "YAML",
    ".yml": "YAML",
}

MANIFEST_NAMES = frozenset(
    {
        "cargo.toml",
        "composer.json",
        "go.mod",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
    }
)


@dataclass(frozen=True, slots=True)
class ScanPolicy:
    max_file_bytes: int = 2_000_000

    def directory_exclusion(
        self, name: str, *, repository_declares_ignores: bool = False
    ) -> str | None:
        folded = name.casefold()
        if folded in EXCLUDED_DIRECTORIES or folded.endswith(EXCLUDED_DIRECTORY_SUFFIXES):
            return "excluded-directory"
        if not repository_declares_ignores and folded in GENERATED_DIRECTORY_NAMES:
            return "generated-directory-name"
        return None

    def file_exclusion(self, path: Path, size_bytes: int) -> str | None:
        name = path.name.casefold()
        suffix = path.suffix.casefold()

        if name == ".env.example":
            pass
        elif name in SENSITIVE_FILE_NAMES or name.startswith(".env."):
            return "sensitive-file-name"

        if suffix in SENSITIVE_SUFFIXES:
            return "sensitive-file-type"
        if suffix in BINARY_SUFFIXES:
            return "known-binary-type"
        if size_bytes > self.max_file_bytes:
            return f"oversized-file:{size_bytes}>{self.max_file_bytes}"
        return None


def classify_language(path: Path) -> str:
    name = path.name.casefold()
    if name == "dockerfile":
        return "Dockerfile"
    if name == "makefile":
        return "Makefile"
    return LANGUAGES_BY_SUFFIX.get(path.suffix.casefold(), "Unknown")


def classify_role(path: Path) -> str:
    name = path.name.casefold()
    parts = {part.casefold() for part in path.parts}
    suffix = path.suffix.casefold()

    if ".github" in parts and "workflows" in parts:
        return "workflow"
    if name in MANIFEST_NAMES or name.endswith(".lock"):
        return "manifest"
    # Every ecosystem spells this differently and getting it wrong is
    # expensive in both directions: a suite read as production code reports
    # its fixtures as the served surface, and a suite invisible as a suite
    # reports every capability as reached by nothing.
    #
    # `_test.` is mandatory in Go and common in Python; `.spec.` is the Jest
    # and Angular convention; `_spec.` is RSpec's. All three were missing, so
    # `handler_test.go`, `app.spec.ts` and `models_spec.rb` were `source`.
    # `selftest` is a harness a project runs against itself.
    # A directory that says a person runs this outranks a filename that
    # merely looks like a suite. `scripts/smoke_test.py` is named like a test
    # and contains none: 417 lines of argparse and a hand-rolled `check`
    # helper, from which a runner would collect nothing. Classifying it by
    # name alone withdrew the true claim that the repository has no
    # conventional test files, which the benchmark caught as lost recall.
    operator_directory = bool({"scripts", "tools", "bin"} & parts)
    named_like_a_test = (
        name.startswith("test_")
        or ".test." in name
        or "_test." in name
        or ".spec." in name
        or "_spec." in name
        or "selftest" in name
    )
    # `fixtures/` is test material wherever it sits. A compiler keeps its
    # negative cases there -- files deliberately malformed so a diagnostic
    # fires -- and reading them as product source made a language project
    # report 24 compiler errors "in the analyzed sources" when every one was a
    # fixture doing its job and the real programs had none. Classifying them
    # `test` is not a new concept: every consumer that already distinguishes a
    # suite from the system handles them correctly the moment they say so.
    if {"test", "tests", "fixtures"} & parts:
        return "test"
    # Code that exercises or demonstrates the product rather than being it.
    # Not `test`: these assert nothing, and counting them as a suite would
    # inflate every statement about what verifies this repository. Not
    # `source` either, which is what they were -- 13 benchmark scripts
    # supplied 13 of this repository's "application entry points", and five
    # of them became capabilities that no test reaches, inflating the one
    # number the summary leads with.
    #
    # `scripts`, `tools` and `bin` are deliberately absent. A hand-run script
    # there is often the real quality gate, `_exercising_paths` already treats
    # it as one, and demoting it would withdraw that.
    if {"benchmarks", "bench", "benches", "examples", "example", "demo", "demos"} & parts:
        return "harness"
    if named_like_a_test and not operator_directory:
        return "test"
    if suffix in {".md", ".rst"} or "docs" in parts or "documentation" in parts:
        return "documentation"
    if suffix in {".toml", ".yaml", ".yml", ".json", ".xml"}:
        return "configuration"
    if suffix in {".sql", ".csv", ".tsv"}:
        return "data"
    if suffix in LANGUAGES_BY_SUFFIX:
        return "source"
    return "unknown"


# Roles whose files are the system rather than something that exercises or
# describes it. Used where a claim asserts a property *of the product*, so
# that a fact true of a benchmark is not filed as a fact about the program.
PRODUCT_ROLES = frozenset({"source"})
# Roles whose code exists to exercise or demonstrate the system rather than
# form part of the system itself. This is deliberately not the inverse of
# `PRODUCT_ROLES`: a manifest or workflow is neither product source nor a test
# harness, and calling one "test-only evidence" is as misleading as calling a
# benchmark the application.
EXERCISING_ROLES = frozenset({"test", "harness"})


# Categories that describe the system when the evidence is source and describe
# the suite when it is a test. Nothing is dropped: a fixture's shape is a real
# fact about the suite, and a reader deciding what the system stores needs to
# know which of the two they are looking at.
TEST_SCOPED_CATEGORIES = {
    "storage": "test_storage",
    "storage_schema": "test_storage_schema",
    "configuration_read": "test_configuration_read",
    "schema_migration": "test_schema_migration",
    "http_route": "test_route",
    "external_call": "test_external_call",
    # What a suite absorbs is not the program's error contract. This
    # repository reported "1 handler(s) catch `OSError, ValueError`"
    # from a test's own `except` around a file it was deliberately
    # failing to write, and the audit flagged it as production error
    # handling evidenced only by tests. The audit was right; the claim
    # should never have carried that category in the first place.
    "caught_exception": "test_caught_exception",
    "exception_type": "test_exception_type",
    "collection_driven_workset": "test_collection_driven_workset",
}

# Re-filing belongs in one place per analyzer, not at each call site.
# Doing it per category is how the same mistake kept reappearing: routes
# were fixed, then schemas, and durable storage was still six-sevenths test
# fixtures. A category added later inherits the behaviour instead of having
# to remember it.


def scoped_category(category: str, role: str) -> str:
    """The category a claim belongs under, given the role of its evidence.

    A fixture's shape is a real fact about the suite and a false one about
    the system. Nothing is dropped; it is filed where a reader asking what
    the system stores will not mistake it for an answer.
    """

    if role != "test":
        return category
    return TEST_SCOPED_CATEGORIES.get(category, category)


def describes_the_product(role: str | None) -> bool:
    """Whether a claim sourced from this file is about the system itself.

    An entry point is the motivating case. `benchmarks/run_comparison.py`
    genuinely defines a `__main__` guard, so reporting it was not false -- but
    filed under "how this application starts" it answered a question nobody
    asked, and thirteen of them buried the five real answers. The predicate is
    shared so that four analyzers cannot drift on what counts as the product.
    """

    return str(role or "") in PRODUCT_ROLES


def exercises_the_product(role: str | None) -> bool:
    """Whether a file runs against or demonstrates the system under review."""

    return str(role or "") in EXERCISING_ROLES
