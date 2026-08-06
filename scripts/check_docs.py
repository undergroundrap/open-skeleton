# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

r"""Fail when a documented command carries an interpreted escape sequence.

This defect has been introduced and repaired four times. Writing
`C:\path\to\repo` through a tool that interprets escapes turns `\t` into a
tab, `\r` into a carriage return and `\f` into a form feed, so a copy-pasteable
command silently becomes `C:<TAB>o<CR>epo` and every reader who trusts the
quick-start hits it first.

A carriage return that is part of a CRLF line ending is fine and expected on
Windows checkouts. A lone one is not, and neither is a tab or a form feed
inside prose.

    python scripts/check_docs.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = frozenset({".git", ".venv", "node_modules", ".open-skeleton", "build", "dist"})
# A tab, form feed, vertical tab, backspace or bell has no business in prose;
# each is what one of the common escape sequences decays into.
FORBIDDEN = {7: "\a", 8: "\b", 9: "\t", 11: "\v", 12: "\f"}


def main() -> int:
    failures: list[str] = []
    for path in sorted(ROOT.rglob("*.md")):
        if SKIP & set(path.relative_to(ROOT).parts):
            continue
        raw = path.read_bytes()
        for index, byte in enumerate(raw):
            if byte in FORBIDDEN or (byte == 13 and raw[index + 1 : index + 2] != b"\n"):
                start = raw.rfind(b"\n", 0, index) + 1
                end = raw.find(b"\n", index)
                line = raw[start : end if end >= 0 else len(raw)].decode("utf-8", "replace")
                name = FORBIDDEN.get(byte, "a lone carriage return")
                failures.append(
                    f"{path.relative_to(ROOT).as_posix()}: {name} in {line.strip()[:70]!r}"
                )
                break

    for failure in failures:
        print(failure)
    if failures:
        print(
            f"\n{len(failures):,} file(s) contain an interpreted escape sequence. "
            "A documented command that cannot be copied is worse than no command."
        )
        return 1
    print("No documented command carries an interpreted escape sequence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
