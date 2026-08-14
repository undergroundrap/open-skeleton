# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

from typing import Protocol

from open_skeleton.models import AnalysisResult, Snapshot

# What an analyzer counts as an eligible file, which decides what a low yield
# means about it. The two readings are not interchangeable and the document
# says different things about each:
#
#   "language"  Eligible is every file of the declared language. A file that
#               parsed and produced nothing is a real limit -- the grammar was
#               handled and the claim vocabulary had nothing to say -- so a low
#               yield belongs in "Where this analysis is thin".
#
#   "subject"   Eligible is every file carrying the thing this analyzer reads:
#               DDL, a published figure, a manifest. A repository where the
#               subject is simply absent must report zero eligible files, not
#               a hundred eligible files and no claims. Reporting the latter
#               put a measurement reader under the thin-coverage warning for
#               the crime of correctly finding no benchmarks in a tutorial.
#
# The distinction is declared rather than inferred because a wrong reading is
# invisible in the output: the claims are identical either way and only the
# denominator moves.
ELIGIBILITY_KINDS = frozenset({"language", "subject"})


class Analyzer(Protocol):
    name: str
    version: str
    eligibility: str

    def analyze(self, snapshot: Snapshot) -> AnalysisResult:
        """Analyze an immutable snapshot without executing target code."""
