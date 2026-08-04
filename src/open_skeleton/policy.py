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
        "coverage",
        "htmlcov",
        "dist",
        "build",
        "target",
        "vendor",
    }
)

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
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
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
    ".php": "PHP",
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

    def directory_exclusion(self, name: str) -> str | None:
        if name.casefold() in EXCLUDED_DIRECTORIES:
            return "excluded-directory"
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
    if "test" in parts or "tests" in parts or name.startswith("test_") or ".test." in name:
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

