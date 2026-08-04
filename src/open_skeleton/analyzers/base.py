# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

from typing import Protocol

from open_skeleton.models import AnalysisResult, Snapshot


class Analyzer(Protocol):
    name: str
    version: str

    def analyze(self, snapshot: Snapshot) -> AnalysisResult:
        """Analyze an immutable snapshot without executing target code."""
