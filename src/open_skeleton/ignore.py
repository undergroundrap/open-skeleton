# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Which paths a repository states are not part of itself.

Every repository already answers "which of these directories did a person
write?" -- in `.gitignore`, in a documented format, written by its own
authors. A fixed exclusion list cannot: it has to be extended once per
ecosystem, and until it is, generated output is read as source.

The cost of not reading it is not subtle. A Unity project in this corpus
scanned 2,538 files, of which 671 of its 683 C# files sat in `Library/` --
Unity's build cache, named on the first line of the project's own
`.gitignore`. Every yield-per-file number for that repository described a
cache directory.

Reading the file rather than enumerating ecosystems is also the difference
between covering Unity, .NET, Go, Elixir and whatever comes next, and
covering the ones somebody remembered.

Limit worth stating: git does not ignore a file it already tracks, however
the patterns read. Determining that requires the index, which is not
consulted here, so a tracked-but-ignored file is dropped where git would
keep it. That case is rare and always deliberate; the exclusion is recorded
with the pattern responsible, so it is visible rather than silent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

IGNORE_FILE_NAME = ".gitignore"


def _translate(pattern: str) -> str:
    """Convert one gitignore glob to a regular expression body.

    `fnmatch.translate` is not usable here: its `*` crosses `/`, which would
    make `build/*` match `build/a/b/c`, and its character classes do not
    reproduce git's. Since `[Ll]ibrary/` is how a real Unity project spells
    its cache directory, the classes have to work.
    """

    out: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        character = pattern[index]
        if character == "*":
            if pattern.startswith("**", index):
                # `**` spans directories; a trailing slash is consumed with it
                # so `a/**/b` still matches `a/b`.
                if pattern.startswith("**/", index):
                    out.append("(?:.*/)?")
                    index += 3
                    continue
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
            index += 1
            continue
        if character == "?":
            out.append("[^/]")
            index += 1
            continue
        if character == "[":
            end = index + 1
            if end < length and pattern[end] in "!^":
                end += 1
            if end < length and pattern[end] == "]":
                end += 1
            while end < length and pattern[end] != "]":
                end += 1
            if end >= length:
                # An unclosed bracket is a literal `[` in git, not an error.
                out.append(re.escape("["))
                index += 1
                continue
            body = pattern[index + 1 : end]
            if body.startswith(("!", "^")):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            index = end + 1
            continue
        if character == "\\" and index + 1 < length:
            out.append(re.escape(pattern[index + 1]))
            index += 2
            continue
        out.append(re.escape(character))
        index += 1
    return "".join(out)


@dataclass(frozen=True, slots=True)
class IgnoreRule:
    """One pattern, kept beside the text that produced it.

    The source text travels with the compiled rule so an exclusion can name
    the line responsible. A file vanishing from a census without a reason is
    the failure mode this whole module has to avoid causing.
    """

    source: str
    matcher: re.Pattern[str]
    negated: bool
    directory_only: bool

    def matches(self, relative_path: str, *, is_dir: bool) -> bool:
        if self.directory_only and not is_dir:
            return False
        return self.matcher.match(relative_path) is not None


def _compile(line: str, base: str) -> IgnoreRule | None:
    """Compile one line, or None when it states nothing."""

    if not line.strip() or line.lstrip().startswith("#"):
        return None

    text = line
    # Trailing spaces are insignificant unless escaped; leading ones are not
    # stripped by git at all, so only the tail is touched.
    if not text.endswith("\\ "):
        text = text.rstrip()
    if not text:
        return None

    negated = text.startswith("!")
    if negated:
        text = text[1:]
    if text.startswith("\\"):
        text = text[1:]

    directory_only = text.endswith("/")
    if directory_only:
        text = text[:-1]
    if not text:
        return None

    # A pattern with an interior slash is anchored to the directory holding
    # the file that declared it; one without matches at any depth below it.
    anchored = "/" in text
    if text.startswith("/"):
        text = text[1:]

    prefix = f"{base}/" if base else ""
    body = _translate(text)
    expression = f"{re.escape(prefix)}{body}" if anchored else f"{re.escape(prefix)}(?:.*/)?{body}"
    return IgnoreRule(
        source=line.strip(),
        matcher=re.compile(f"^{expression}$"),
        negated=negated,
        directory_only=directory_only,
    )


@dataclass(frozen=True, slots=True)
class IgnoreRules:
    """The rules in force at one point in a walk."""

    rules: tuple[IgnoreRule, ...] = ()

    @property
    def declared(self) -> bool:
        return bool(self.rules)

    def extended(self, directory: Path, base: str) -> IgnoreRules:
        """Add the `.gitignore` in ``directory``, if it has one.

        Nested ignore files are read because that is where a monorepo puts
        the rules for a subproject, and a reader that only looked at the root
        would treat those subprojects the way the root list treats Unity.
        """

        candidate = directory / IGNORE_FILE_NAME
        try:
            source = candidate.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            return self
        added = tuple(
            rule for rule in (_compile(line, base) for line in source.splitlines()) if rule
        )
        return IgnoreRules(rules=(*self.rules, *added)) if added else self

    def excluded_by(self, relative_path: str, *, is_dir: bool) -> str | None:
        """The last pattern that excludes this path, or None if it survives.

        Later rules win, which is what makes `!keep.txt` after `*.txt` work.
        A negated rule does not report a reason because it is the absence of
        one.
        """

        reason: str | None = None
        for rule in self.rules:
            if rule.matches(relative_path, is_dir=is_dir):
                reason = None if rule.negated else rule.source
        return reason
