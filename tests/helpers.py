# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

from pathlib import Path


def create_sample_repository(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "web").mkdir()
    (root / "tests").mkdir()
    (root / "node_modules" / "ignored-package").mkdir(parents=True)

    (root / "README.md").write_text("# Sample\n\nA fixture.\n", encoding="utf-8")
    (root / "package.json").write_text('{"name":"sample"}\n', encoding="utf-8")
    (root / "src" / "app.py").write_text(
        "def answer() -> int:\n    return 42\n",
        encoding="utf-8",
    )
    (root / "web" / "app.ts").write_text(
        "export const answer: number = 42;\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_app.py").write_text(
        "from src.app import answer\n\ndef test_answer():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    (root / "node_modules" / "ignored-package" / "index.js").write_text(
        "throw new Error('must not be read');\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("FAKE_TEST_TOKEN=not-a-secret\n", encoding="utf-8")
    (root / "payload.dat").write_bytes(b"text-prefix\x00binary-tail")

