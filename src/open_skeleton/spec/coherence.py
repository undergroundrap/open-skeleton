# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Whether a specification tells the truth about itself.

`verify.py` answers a different question: it re-resolves every citation
against the ledger and the current sources, so it catches a receipt that
points at nothing. It cannot catch a document that cites perfectly and still
contradicts itself.

That distinction is not theoretical. Runtime Topology once read
"Determination: absent. Every probe declared for this concern returned zero
matches" directly above a table of seven verified findings, and the executive
summary counted it among the concerns the repository does not implement. Every
automated check passed while it did -- each claim carried a receipt and
citation integrity was 100%. Four of five single repositories had the same
defect, and one of them was this one, where the entire error-contract analysis
printed twenty-four findings under a heading announcing their absence.

The checks here read the rendered document the way a person would and compare
what it *says* against the data it was projected from. A count that disagrees
with the list beneath it, a verdict that disagrees with the findings beneath
it, a table whose rows do not sum to the thing they partition: each of those
was a real defect found by reading, and each is now a check that runs on any
repository.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from open_skeleton.spec.render import SpecDocument

# Verdicts whose sentence tells a reader the concern was not found. A section
# carrying one of these may not also render findings, because the findings are
# the evidence that it was.
ABSENCE_VERDICTS = frozenset({"absent", "not_applicable"})

# "21 of 48" and "34 of 60 probed concerns returned no matches" both introduce
# a list, so the count and the list may not be adjacent.
_ENUMERATION = re.compile(r"(?P<shown>[\d,]+) of (?P<total>[\d,]+)\b")
# Three or more quoted or numbered items is what distinguishes a list from a
# sentence that happens to mention a ratio.
MIN_ENUMERATED_ITEMS = 3
_REMAINDER = re.compile(r"and (?P<count>[\d,]+) more")
_SUMMARY_ROW = re.compile(r"^\|\s*(?P<verdict>[a-z_]+)\s*\|\s*(?P<count>[\d,]+)\s*\|$")


@dataclass(frozen=True, slots=True)
class Incoherence:
    """One way the document disagrees with itself."""

    check: str
    detail: str

    def __str__(self) -> str:
        return f"{self.check}: {self.detail}"


def _number(text: str) -> int:
    return int(text.replace(",", ""))


def _verdict_contradictions(document: SpecDocument) -> list[Incoherence]:
    """A section may not announce absence above its own evidence."""

    found: list[Incoherence] = []
    for section in document.sections:
        if section.verdict in ABSENCE_VERDICTS and section.findings:
            found.append(
                Incoherence(
                    "verdict-contradicts-findings",
                    f"§{section.number} {section.title} reports `{section.verdict}` "
                    f"and renders {len(section.findings):,} finding(s) beneath it.",
                )
            )
    return found


def _claim_routing(document: SpecDocument) -> list[Incoherence]:
    """Every rendered claim reaches exactly one section."""

    seen: dict[str, str] = {}
    found: list[Incoherence] = []
    for section in document.sections:
        for claim in section.findings:
            first = seen.get(claim.claim_id)
            if first is not None:
                found.append(
                    Incoherence(
                        "claim-rendered-twice",
                        f"{claim.claim_id[:8]} appears in §{first} and §{section.number}.",
                    )
                )
            else:
                seen[claim.claim_id] = section.number
    return found


def _determination_summary(document: SpecDocument, markdown: str) -> list[Incoherence]:
    """The verdict table partitions the sections, so its rows must sum to them."""

    if "## Determination summary" not in markdown:
        return []
    table = markdown.split("## Determination summary", 1)[1].split("\n## ", 1)[0]
    counted = 0
    listed: set[str] = set()
    for line in table.splitlines():
        match = _SUMMARY_ROW.match(line.strip())
        if match is None:
            continue
        counted += _number(match.group("count"))
        listed.add(match.group("verdict"))
    if counted != len(document.sections):
        missing = sorted({item.verdict for item in document.sections} - listed)
        detail = (
            f"rows sum to {counted:,} of {len(document.sections):,} section(s)"
            f"{'; absent from the table: ' + ', '.join(missing) if missing else ''}."
        )
        return [Incoherence("determination-summary-incomplete", detail)]
    return []


def _enumerations(markdown: str) -> list[Incoherence]:
    """A stated count and the list under it must agree, or say why they do not.

    The untraced-capability sentence read "21 of 48" and then named ten, with
    no ellipsis and no remainder, so a reader stops at the tenth name believing
    it is the whole set. Every single-project run had fewer than ten, which is
    why five repository shapes went by without showing it.
    """

    found: list[Incoherence] = []
    for paragraph in markdown.split("\n\n"):
        match = _ENUMERATION.search(paragraph)
        if match is None:
            continue
        stated = _number(match.group("shown"))
        listed = len(re.findall(r"`[^`]+`", paragraph)) or len(re.findall(r"§[\d.]+", paragraph))
        if listed < MIN_ENUMERATED_ITEMS or listed >= stated:
            continue
        remainder = _REMAINDER.search(paragraph)
        if remainder is None:
            found.append(
                Incoherence(
                    "enumeration-truncated-silently",
                    f"a sentence claims {stated:,} item(s) and names {listed:,} "
                    f"without saying so: {paragraph.strip()[:90]}...",
                )
            )
        elif _number(remainder.group("count")) != stated - listed:
            found.append(
                Incoherence(
                    "enumeration-remainder-wrong",
                    f"a sentence claims {stated:,} item(s), names {listed:,}, and "
                    f"reports {remainder.group('count')} more.",
                )
            )
    return found


def _absence_count(document: SpecDocument, markdown: str) -> list[Incoherence]:
    """The summary's absence tally must match the sections that carry one."""

    marker = "### Concerns this repository does not implement"
    if marker not in markdown:
        return []
    paragraph = markdown.split(marker, 1)[1].split("\n\n")[1]
    match = _ENUMERATION.search(paragraph)
    if match is None:
        return []
    stated = _number(match.group("shown"))
    actual = sum(1 for item in document.sections if item.verdict == "absent")
    if stated != actual:
        return [
            Incoherence(
                "absence-tally-disagrees",
                f"the summary reports {stated:,} concern(s) with no matches and "
                f"{actual:,} section(s) carry an `absent` verdict.",
            )
        ]
    return []


def _capability_tally(document: SpecDocument, markdown: str) -> list[Incoherence]:
    """The untraced tally must match the capabilities that name no verifier."""

    marker = "### Capabilities with no verifying reference"
    if marker not in markdown:
        return []
    paragraph = markdown.split(marker, 1)[1].split("\n\n")[1]
    match = _ENUMERATION.search(paragraph)
    if match is None:
        return []
    stated = _number(match.group("shown"))
    total = _number(match.group("total"))
    untraced = sum(1 for item in document.capabilities if not item.exercised_by)
    found: list[Incoherence] = []
    if stated != untraced:
        found.append(
            Incoherence(
                "capability-tally-disagrees",
                f"the summary reports {stated:,} unverified capability(ies) and "
                f"{untraced:,} name no verifying reference.",
            )
        )
    if total != len(document.capabilities):
        found.append(
            Incoherence(
                "capability-total-disagrees",
                f"the summary reports {total:,} capability(ies) against a catalog "
                f"of {len(document.capabilities):,}.",
            )
        )
    return found


def check_coherence(document: SpecDocument, markdown: str) -> tuple[Incoherence, ...]:
    """Every way this document is found to disagree with itself.

    An empty result is the expected outcome and the only passing one. This
    does not certify the document correct: it certifies that what the prose
    asserts about the data matches the data it was projected from.
    """

    return (
        *_verdict_contradictions(document),
        *_claim_routing(document),
        *_determination_summary(document, markdown),
        *_enumerations(markdown),
        *_absence_count(document, markdown),
        *_capability_tally(document, markdown),
    )
