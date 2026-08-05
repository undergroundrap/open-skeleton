# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""What plays a concern's part when the concern's usual product is absent.

Reporting that a repository has no message broker is true and incomplete. The
work still has to happen somewhere, and it does: a list appended to and
trimmed is a queue with a capacity and a loss mode, a constant compared
against in a guard is a threshold whether or not anything pages on it, and a
dict with a size cap is a cache with an eviction policy.

An absent verdict that stops at the absence sends a reader to grep. This layer
names the structure that stands in, so the section says what the system does
instead of only what it does not have.

Two limits are deliberate and stated in every rendering. A substitute is a
structural resemblance, not an equivalence: the structures named here have
none of the durability or delivery guarantees of the product they stand in
for, which is usually the most important thing about them. And nothing here
recommends adopting the real product — that is an engineering decision this
document has no standing to make.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Guard labels are rendered source, so a threshold is recognised by its name
# appearing in a comparison rather than by any type information.
COMPARISONS = ("<", ">", "==", "!=", "<=", ">=", " in ", " not in ")


@dataclass(frozen=True, slots=True)
class SubstituteStructure:
    """One structure standing in for an absent concern."""

    name: str
    location: str
    role: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "location": self.location, "role": self.role}


@dataclass(frozen=True, slots=True)
class Substitute:
    """An absent concern, and what the repository uses in its place."""

    rule_id: str
    concern: str
    statement: str
    caveat: str
    structures: tuple[SubstituteStructure, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "concern": self.concern,
            "statement": self.statement,
            "caveat": self.caveat,
            "structures": [item.to_dict() for item in self.structures],
        }


def _pending_state(
    symbols: tuple[dict[str, Any], ...],
    claims: tuple[dict[str, Any], ...],
) -> tuple[SubstituteStructure, ...]:
    """Containers written to at runtime: where work waits when nothing queues it.

    Being initialised to a mutable literal is not enough, and using it as the
    test produced exactly the wrong answer: a lookup table of room names is a
    dict that never changes, and calling it a place work waits in is a
    fabricated claim about a constant.

    What makes a container queue-like is that something puts things into it
    while the process runs. That is what the process-local-state claim records,
    so this reads those claims rather than re-deriving the property.
    """

    located = {
        str(symbol.get("qualified_name", "")): f"{symbol['path']}:{symbol.get('start_line', 1)}"
        for symbol in symbols
        if symbol.get("qualified_name")
    }
    found: list[SubstituteStructure] = []
    for claim in claims:
        if claim.get("category") != "process_local_state":
            continue
        text = str(claim.get("claim", ""))
        name = text.split(" is a module-owned", 1)[0].strip()
        if not name:
            continue
        found.append(
            SubstituteStructure(
                name=name,
                location=located.get(name, "—"),
                role=(
                    "written to while the process runs, so anything waiting in it "
                    "lives in one process and is lost when that process stops"
                ),
            )
        )
    return tuple(sorted(found, key=lambda item: item.name))


def _bounded_containers(symbols: tuple[dict[str, Any], ...]) -> tuple[SubstituteStructure, ...]:
    """Literal tables with a size: a fixed capacity is a buffer's defining fact."""

    found: list[SubstituteStructure] = []
    for symbol in symbols:
        containers = (symbol.get("metadata") or {}).get("data_containers") or {}
        for name, entry in sorted(containers.items()):
            found.append(
                SubstituteStructure(
                    name=name,
                    location=f"{symbol['path']}:{entry['line']}",
                    role=(
                        f"{entry['kind']} literal holding {int(entry['size']):,} entries, "
                        "a fixed capacity decided in source rather than by configuration"
                    ),
                )
            )
    return tuple(found)


def _evaluated_thresholds(symbols: tuple[dict[str, Any], ...]) -> tuple[SubstituteStructure, ...]:
    """Named constants a guard actually compares against.

    A constant nothing branches on is a value. One that decides a branch is a
    threshold, and the branch is where the system reacts to crossing it — which
    is the part alerting infrastructure would otherwise own.
    """

    tunables: dict[str, tuple[str, str]] = {}
    for symbol in symbols:
        for name, entry in ((symbol.get("metadata") or {}).get("tunables") or {}).items():
            bare = str(name).rsplit(".", 1)[-1]
            value = entry.get("value")
            rendered = (
                str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
            )
            tunables.setdefault(bare, (rendered, f"{symbol['path']}:{entry.get('line', 1)}"))

    found: list[SubstituteStructure] = []
    seen: set[tuple[str, str]] = set()
    for symbol in symbols:
        for event in (symbol.get("metadata") or {}).get("control_flow") or []:
            if event.get("kind") != "guard":
                continue
            label = str(event.get("label", ""))
            if not any(token in label for token in COMPARISONS):
                continue
            for name, (value, declared_at) in tunables.items():
                if name not in label:
                    continue
                site = f"{symbol['path']}:{event.get('line', 1)}"
                if (name, site) in seen:
                    continue
                seen.add((name, site))
                found.append(
                    SubstituteStructure(
                        name=f"{name} = {value}",
                        location=site,
                        role=(
                            f"declared at {declared_at} and compared in `{label}`, so "
                            "crossing it changes what this code does"
                        ),
                    )
                )
    return tuple(sorted(found, key=lambda item: (item.location, item.name)))


# Each rule names a concern, the profile sections whose absence triggers it, and the
# collector that finds what stands in. Adding one means adding a collector and
# an entry here; the rule itself asserts nothing the collector did not find.
STANDARD_RULES: tuple[tuple[str, str, frozenset[str], str, str], ...] = (
    (
        "queue-without-broker",
        "message queue",
        frozenset({"integration.messaging"}),
        (
            "No message broker is declared. Whatever queuing, buffering or "
            "hand-off this system performs therefore happens in one of the "
            "process-local containers below, which are every container written "
            "to while the process runs. This is the candidate set, not a claim "
            "that each one is a queue: a cooldown table and a mailbox both "
            "appear here and only one of them carries messages."
        ),
        (
            "These resemble queues in shape only. None offers acknowledgement, "
            "redelivery, ordering across processes, or durability, and a reader "
            "should assume a message held here is lost when the process stops."
        ),
    ),
    (
        "threshold-without-alerting",
        "alerting thresholds",
        frozenset({"operations.observability", "integration.request-throttling"}),
        (
            "No alerting or telemetry product is declared. These constants are "
            "nevertheless compared in guards, so crossing one already changes "
            "behaviour — the reaction exists, only the notification does not."
        ),
        (
            "A guard is not an alert. Crossing one of these changes a code path "
            "and notifies nobody, and no operator learns of it unless something "
            "outside this repository is watching."
        ),
    ),
    (
        "buffer-without-cache",
        "cache",
        frozenset({"state.caching"}),
        (
            "No cache server is declared. These fixed-size in-process structures "
            "hold what a cache would otherwise hold."
        ),
        (
            "Capacity here is decided in source rather than by configuration, and "
            "the contents are per-process: two instances of this system share "
            "none of it and can disagree indefinitely."
        ),
    ),
)

_COLLECTORS: dict[str, Any] = {
    "queue-without-broker": _pending_state,
    "threshold-without-alerting": lambda symbols, _claims: _evaluated_thresholds(symbols),
    "buffer-without-cache": lambda symbols, _claims: _bounded_containers(symbols),
}
MAX_STRUCTURES = 12


def derive_substitutes(
    symbols: tuple[dict[str, Any], ...],
    claims: tuple[dict[str, Any], ...] = (),
    *,
    absent_sections: frozenset[str] = frozenset(),
) -> tuple[Substitute, ...]:
    """Name what stands in for each concern a probe found absent.

    A rule fires only when its concern is genuinely absent and its collector
    actually found something. A rule that fires with nothing to show would be
    asserting a substitute exists on the strength of the concern being missing,
    which is the reasoning this whole engine exists to avoid.
    """

    results: list[Substitute] = []
    for rule_id, concern, triggers, statement, caveat in STANDARD_RULES:
        if not (triggers & absent_sections):
            continue
        structures = _COLLECTORS[rule_id](symbols, claims)
        if not structures:
            continue
        results.append(
            Substitute(
                rule_id=rule_id,
                concern=concern,
                statement=statement,
                caveat=caveat,
                structures=structures[:MAX_STRUCTURES],
            )
        )
    return tuple(results)
