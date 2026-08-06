# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Outline-driven projection of the evidence ledger into a technical specification.

The subsystem never generates claims. It selects claims that already exist in the
ledger, decides whether each outline concern is present in the analyzed repository
using re-runnable probes, and renders the result. Absence is a first-class verdict
backed by the exact query that found nothing.
"""

from open_skeleton.spec.profile import (
    SpecProbe,
    SpecProfile,
    SpecSection,
    default_profile_path,
    load_profile,
)
from open_skeleton.spec.render import (
    SpecDocument,
    build_spec,
    render_spec_index_json,
    render_spec_json,
    render_spec_markdown,
)
from open_skeleton.spec.verify import CitationReport, verify_spec

__all__ = [
    "CitationReport",
    "SpecDocument",
    "SpecProbe",
    "SpecProfile",
    "SpecSection",
    "build_spec",
    "default_profile_path",
    "load_profile",
    "render_spec_index_json",
    "render_spec_json",
    "render_spec_markdown",
    "verify_spec",
]
