# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""A repository may hold text that a UTF-8 document cannot.

`pygments/unistring.py` names the Unicode categories, and the category of
surrogates is spelled with surrogates. Those code points cannot be encoded as
UTF-8 by definition, so carrying one into the document ended the run at the
last step: every one of 339 files was read correctly and `spec.json` was
written empty. A specification generator that dies on a Unicode table is not
general, and this is the shape of repository that proves it.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.models import ClaimRecord, SymbolRecord, utc_now, writable_text
from open_skeleton.scanner import scan_repository
from open_skeleton.spec import build_spec, load_profile, render_spec_markdown

# Written the way the source writes it: an escape in an ASCII file, which
# becomes a real lone surrogate once the literal is read.
SURROGATE = "\ud800"


def _encodable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class WritableTextTests(TestCase):
    def test_ordinary_text_is_returned_unchanged(self) -> None:
        for value in ("plain", "accented café", "punctuation — dash", "日本語", ""):
            self.assertEqual(writable_text(value), value)

    def test_a_lone_surrogate_becomes_the_escape_the_source_wrote(self) -> None:
        self.assertEqual(writable_text(SURROGATE), "\\ud800")
        self.assertTrue(_encodable(writable_text(SURROGATE)))

    def test_the_surrounding_text_survives(self) -> None:
        # Nothing is dropped: the value is still readable, and the escape is
        # the truer rendering anyway, since printing the code point was
        # already a transformation this engine chose to make.
        self.assertEqual(writable_text(f"Cs = {SURROGATE}-x"), "Cs = \\ud800-x")


class RecordsHoldWritableTextTests(TestCase):
    def test_a_claim_cannot_carry_text_a_document_cannot_hold(self) -> None:
        claim = ClaimRecord(
            claim_id="c1",
            snapshot_id="s",
            claim=f"declares {SURROGATE} as a member",
            category="value_set",
            status="verified",
            confidence=1.0,
            importance="medium",
            produced_by="test",
            created_at=utc_now(),
            supporting_evidence=("e1",),
        )
        self.assertTrue(_encodable(claim.claim))
        self.assertIn("\\ud800", claim.claim)

    def test_symbol_metadata_is_covered_too(self) -> None:
        # The first fix covered claim text alone and the run died at the same
        # character: the constant is recorded twice, and the panel is built
        # from the metadata rather than from the prose.
        symbol = SymbolRecord(
            symbol_id="s1",
            snapshot_id="s",
            path="unistring.py",
            qualified_name="unistring",
            kind="module",
            start_line=1,
            end_line=1,
            language="Python",
            analyzer="test",
            metadata={"constants": {"Cs": [f"{SURROGATE}-x", {"nested": SURROGATE}]}},
        )
        self.assertTrue(_encodable(json.dumps(symbol.metadata, ensure_ascii=False)))


class UnicodeTableRepositoryTests(TestCase):
    """End to end, on the shape that actually broke."""

    SOURCE = (
        "# A Unicode category table, as a real one is written.\n"
        "Cc = '\\x00-\\x1f'\n"
        "Cs = '\\ud800-\\udbff\\udc00-\\udfff'\n"
        "CATEGORIES = {'Cc': Cc, 'Cs': Cs}\n"
    )

    def test_a_specification_is_produced_and_can_be_written(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "unistring.py").write_text(self.SOURCE, encoding="utf-8")
            snapshot = scan_repository(root)
            result = analyze_snapshot(snapshot)

            self.assertTrue(result.claims, "the file should still be read")
            for claim in result.claims:
                self.assertTrue(_encodable(claim.claim))
            for symbol in result.symbols:
                self.assertTrue(_encodable(json.dumps(symbol.metadata, ensure_ascii=False)))

            # Guards this class against passing because nothing reached the
            # surrogate at all. A comparison between two clean answers proves
            # nothing, and this suite has been fooled that way before.
            carried = [
                symbol.qualified_name
                for symbol in result.symbols
                if "\\ud800" in json.dumps(symbol.metadata, ensure_ascii=False)
            ]
            self.assertEqual(carried, ["unistring"])

    def test_the_rendered_document_encodes(self) -> None:
        from open_skeleton.ledger import EvidenceLedger

        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            (root / "unistring.py").write_text(self.SOURCE, encoding="utf-8")
            snapshot = scan_repository(root)
            ledger = EvidenceLedger(Path(temporary) / "evidence.sqlite3")
            ledger.save_snapshot(snapshot)
            ledger.save_analysis(analyze_snapshot(snapshot))
            document = build_spec(ledger, load_profile())

            self.assertTrue(_encodable(render_spec_markdown(document)))
            self.assertTrue(_encodable(json.dumps(document.to_dict(), ensure_ascii=False)))
