# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from typing import Any
from unittest import TestCase

from open_skeleton.spec.capabilities import (
    Capability,
    _short_name,
    build_capabilities,
    verifying_paths,
)


def _claim(
    claim_id: str, category: str, text: str, evidence: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "category": category,
        "claim": text,
        "supporting_evidence": list(evidence),
    }


class CapabilityClusteringTests(TestCase):
    FILES = (
        {"path": "backend/main.py", "role": "source"},
        {"path": "backend/util.py", "role": "source"},
        {"path": "tests/test_api.py", "role": "test"},
        {"path": "scripts/smoke.py", "role": "source"},
    )
    SYMBOLS = (
        {
            "qualified_name": "backend.main.attack",
            "kind": "async_function",
            "path": "backend/main.py",
        },
        {
            "qualified_name": "backend.main.move",
            "kind": "async_function",
            "path": "backend/main.py",
        },
        {
            "qualified_name": "backend.main.get_player",
            "kind": "async_function",
            "path": "backend/main.py",
        },
        {"qualified_name": "backend.util.helper", "kind": "function", "path": "backend/util.py"},
        {"qualified_name": "scripts.smoke.run", "kind": "function", "path": "scripts/smoke.py"},
    )
    CLAIMS = (
        _claim(
            "r1",
            "http_route",
            "POST /action/attack/{player_id} is handled by backend.main.attack.",
            ("e1",),
        ),
        _claim(
            "r2",
            "http_route",
            "POST /action/move/{player_id} is handled by backend.main.move.",
            ("e2",),
        ),
        _claim(
            "r3",
            "http_route",
            "GET /player/{player_id} is handled by backend.main.get_player.",
            ("e3",),
        ),
        _claim("h1", "operator_harness", "scripts/smoke.py is an operator harness.", ("e4",)),
    )
    EVIDENCE = {
        "e1": {"path": "backend/main.py"},
        "e2": {"path": "backend/main.py"},
        "e3": {"path": "backend/main.py"},
        "e4": {"path": "scripts/smoke.py"},
    }

    def _build(self, edges: tuple[dict[str, Any], ...]) -> tuple[Capability, ...]:
        return build_capabilities(
            files=self.FILES,
            claims=self.CLAIMS,
            symbols=self.SYMBOLS,
            edges=edges,
            evidence_by_id=self.EVIDENCE,
        )

    def test_routes_cluster_by_leading_static_segment(self) -> None:
        capabilities = self._build(())
        labels = {item.label: item for item in capabilities if item.kind == "route-group"}
        self.assertEqual(set(labels), {"action", "player"})
        self.assertEqual(len(labels["action"].routes), 2)
        self.assertEqual(len(labels["player"].routes), 1)

    def test_a_route_claim_that_says_more_still_clusters(self) -> None:
        # The handler is a dotted name and the claim may continue after it.
        # This pattern was anchored to the end of the string with a greedy
        # `.+`, so adding one sentence to the route claim -- the HTTP statuses
        # it names -- swallowed that sentence into the handler. Clustering by
        # module then failed and four capabilities vanished from a document,
        # with nothing reporting the loss.
        verbose = (
            *self.CLAIMS,
            _claim(
                "r9",
                "http_route",
                "GET /player/{player_id}/gear is handled by backend.main.gear. "
                "It names HTTP status `404`, `429` on paths through it; whether each "
                "is reachable is not decided here.",
                ("e1",),
            ),
        )
        capabilities = build_capabilities(
            files=self.FILES,
            claims=verbose,
            symbols=self.SYMBOLS,
            edges=(),
            evidence_by_id=self.EVIDENCE,
        )
        plain = (
            *self.CLAIMS,
            _claim(
                "r9",
                "http_route",
                "GET /player/{player_id}/gear is handled by backend.main.gear.",
                ("e1",),
            ),
        )
        baseline = build_capabilities(
            files=self.FILES,
            claims=plain,
            symbols=self.SYMBOLS,
            edges=(),
            evidence_by_id=self.EVIDENCE,
        )
        # The route is captured either way -- the path is not what broke. What
        # broke is the handler, and through it the clustering, so the assertion
        # has to be that the two documents describe the same capabilities.
        self.assertEqual(
            [(item.kind, item.label, item.routes) for item in capabilities],
            [(item.kind, item.label, item.routes) for item in baseline],
            "a route claim carrying extra prose must cluster exactly as the plain one does",
        )

    def test_a_harness_module_is_not_catalogued_as_a_capability(self) -> None:
        # A benchmark's `main` is not something the product does. Five of this
        # repository's own capabilities were benchmark scripts, and because
        # nothing tests a benchmark all five counted as "implemented but
        # reached by no test", taking the summary's headline from 2 to 7.
        #
        # No filter here does this: capabilities are drawn from `source` files
        # and a harness file simply is not one. The test guards that the role
        # keeps carrying the meaning rather than that a list stays current.
        files = (*self.FILES, {"path": "benchmarks/run_bench.py", "role": "harness"})
        symbols = (
            *self.SYMBOLS,
            {
                "qualified_name": "benchmarks.run_bench.main",
                "kind": "function",
                "path": "benchmarks/run_bench.py",
            },
        )
        capabilities = build_capabilities(
            files=files,
            claims=self.CLAIMS,
            symbols=symbols,
            edges=(),
            evidence_by_id=self.EVIDENCE,
        )
        labels = {item.label for item in capabilities}
        self.assertNotIn("run_bench", labels)
        self.assertNotIn("benchmarks", labels)

    def test_identifiers_are_sequential_with_no_gaps(self) -> None:
        capabilities = self._build(())
        self.assertEqual(
            [item.capability_id for item in capabilities],
            [f"C-{index:03d}" for index in range(1, len(capabilities) + 1)],
        )

    def test_route_literal_prefix_matches_an_interpolated_client_url(self) -> None:
        # A client builds "/action/attack/{pid}" with an f-string, so the recorded
        # literal is only the static prefix.
        capabilities = self._build(
            (
                {
                    "relationship": "references_route_path",
                    "source_path": "scripts/smoke.py",
                    "target_ref": "/action/attack/",
                },
            )
        )
        action = next(item for item in capabilities if item.label == "action")
        self.assertEqual(action.verification, "exercised")
        self.assertTrue(
            any("requests /action/attack/{player_id}" in ref for ref in action.exercised_by)
        )

    def test_unreferenced_capability_reports_no_verifying_reference(self) -> None:
        capabilities = self._build(
            (
                {
                    "relationship": "references_route_path",
                    "source_path": "scripts/smoke.py",
                    "target_ref": "/action/attack/",
                },
            )
        )
        player = next(item for item in capabilities if item.label == "player")
        self.assertEqual(player.verification, "no-verifying-reference")
        self.assertEqual(player.exercised_by, ())

    def test_harness_calling_its_own_helper_is_not_coverage(self) -> None:
        capabilities = self._build(
            (
                {
                    "relationship": "calls",
                    "source_path": "scripts/smoke.py",
                    "target_ref": "run",
                },
            )
        )
        # Labelled `smoke` rather than `scripts`: a folder that says where code
        # was put does not name a capability, and this asserted `scripts` until
        # a browser game's entire source tree was reported as one capability
        # called `src`.
        scripts = [item for item in capabilities if item.label == "smoke"]
        self.assertTrue(scripts)
        self.assertEqual(scripts[0].verification, "no-verifying-reference")

    def test_call_into_another_file_does_count_as_coverage(self) -> None:
        capabilities = self._build(
            (
                {
                    "relationship": "calls",
                    "source_path": "tests/test_api.py",
                    "target_ref": "helper",
                },
            )
        )
        backend = next(item for item in capabilities if item.kind == "module")
        self.assertEqual(backend.verification, "exercised")

    def test_calls_from_non_verifying_files_are_ignored(self) -> None:
        capabilities = self._build(
            (
                {
                    "relationship": "calls",
                    "source_path": "backend/main.py",
                    "target_ref": "helper",
                },
            )
        )
        backend = next(item for item in capabilities if item.kind == "module")
        self.assertEqual(backend.verification, "no-verifying-reference")

    def test_route_handlers_are_not_duplicated_into_module_clusters(self) -> None:
        capabilities = self._build(())
        module_symbols = {
            symbol for item in capabilities if item.kind == "module" for symbol in item.symbols
        }
        self.assertNotIn("backend.main.attack", module_symbols)
        self.assertIn("backend.util.helper", module_symbols)

    def test_verifying_paths_include_test_roles_and_harness_claims(self) -> None:
        paths = verifying_paths(self.FILES, self.CLAIMS, self.EVIDENCE)
        self.assertEqual(paths, frozenset({"tests/test_api.py", "scripts/smoke.py"}))

    def test_output_is_deterministic(self) -> None:
        first = self._build(())
        second = self._build(())
        self.assertEqual([item.to_dict() for item in first], [item.to_dict() for item in second])


class QualifiedNameTests(TestCase):
    """Name normalization across languages that punctuate differently.

    A Rust crate with 178 passing tests reported no verifying reference for any
    capability. Four things were wrong in sequence, and this was the one that
    made the other three invisible: comparing a full path against a bare name
    cannot succeed, so it failed silently and read as absent verification
    rather than as a name that never normalized.
    """

    def test_a_dotted_name_reduces_to_its_last_segment(self) -> None:
        self.assertEqual(_short_name("app.services.billing.charge"), "charge")

    def test_a_rust_path_reduces_to_its_last_segment(self) -> None:
        self.assertEqual(_short_name("crates::core::compat::check_build"), "check_build")

    def test_a_mixed_separator_name_reduces_to_its_last_segment(self) -> None:
        # Module paths are recorded with dots and Rust items with colons, so a
        # qualified name can carry both.
        self.assertEqual(_short_name("crates.core::compat::check_build"), "check_build")

    def test_a_bare_name_is_returned_unchanged(self) -> None:
        self.assertEqual(_short_name("check_build"), "check_build")


class ReferenceStrengthTests(TestCase):
    """A reference says which kind of verifier produced it.

    Whole-file granularity means a source file carrying inline tests counts as
    verifying everything it calls, including from code that is not the test.
    That is real evidence and it is weaker than a dedicated test calling the
    same thing, so the sentence distinguishes them rather than letting a reader
    assume the stronger reading of a number.
    """

    def test_a_dedicated_test_reference_reads_as_a_direct_call(self) -> None:
        capabilities = CapabilityClusteringTests()._build(
            (
                {
                    "relationship": "calls",
                    "source_path": "tests/test_api.py",
                    "target_ref": "attack",
                },
            )
        )
        references = [ref for item in capabilities for ref in item.exercised_by]
        self.assertTrue(references, "the call should have been recorded")
        self.assertFalse(any("contains tests and" in ref for ref in references))


class BuildContainerLabelTests(TestCase):
    """A folder that says where code was put does not name a capability.

    Reading a browser game's specification showed its capability catalogue as
    "2 of 2: `server`, `src`". Every one of sixteen modules -- movement,
    combat, renderer, terrain -- was clustered into one capability named after
    the directory they happened to sit in, and `src` tells a reader nothing
    about what the program does.
    """

    def _labels(self, *paths: str) -> set[str]:
        symbols = tuple(
            {"qualified_name": f"m{index}.run", "kind": "function", "path": path}
            for index, path in enumerate(paths)
        )
        files = tuple({"path": path, "role": "source"} for path in paths)
        return {
            item.label
            for item in build_capabilities(
                files=files, claims=(), symbols=symbols, edges=(), evidence_by_id={}
            )
        }

    def test_a_build_container_yields_module_names(self) -> None:
        found = self._labels("src/movement.js", "src/combat.js", "src/renderer.js")
        self.assertEqual(found, {"movement", "combat", "renderer"})

    def test_a_meaningful_directory_still_names_the_cluster(self) -> None:
        # `backend/` describes a part of the system; `src/` describes a layout.
        found = self._labels("backend/api.py", "backend/models.py")
        self.assertEqual(found, {"backend"})

    def test_a_file_at_the_root_names_itself(self) -> None:
        self.assertEqual(self._labels("server.py"), {"server"})


class AmbiguousCalleeTests(TestCase):
    """A name defined twice does not say which definition was called.

    `main` is defined in sixteen files of this repository and `to_dict` in
    fourteen. Matching a call edge by short name alone therefore credited
    every capability holding that name: the `turn_gate` capability was
    reported as exercised by `tests/test_cli.py calls main`, which is the
    CLI's main, and three of its four references were that shape. Before a
    real test for it existed, the capability still read as covered.

    This is the rule the route reader already follows for an unresolved
    receiver -- when the evidence does not distinguish, make no claim either
    way. The cost is a false negative where a genuinely tested capability
    happens to share a common name, which is the trade this project states:
    a surface naming verification that does not exist is worse than one
    omitting verification that does.
    """

    FILES = (
        {"path": "alpha/service.py", "role": "source"},
        {"path": "beta/service.py", "role": "source"},
        {"path": "tests/test_alpha.py", "role": "test"},
    )

    def _capabilities(self, symbols: tuple[dict[str, Any], ...]) -> tuple[Capability, ...]:
        return build_capabilities(
            files=self.FILES,
            claims=(),
            symbols=symbols,
            edges=(
                {
                    "relationship": "calls",
                    "source_path": "tests/test_alpha.py",
                    "target_ref": "run",
                },
            ),
            evidence_by_id={},
        )

    def test_a_name_defined_once_still_counts_as_verification(self) -> None:
        found = self._capabilities(
            (
                {
                    "qualified_name": "alpha.service.run",
                    "kind": "function",
                    "path": "alpha/service.py",
                },
            )
        )
        exercised = [item for item in found if item.exercised_by]
        self.assertEqual(len(exercised), 1)

    def test_a_name_defined_twice_credits_neither(self) -> None:
        found = self._capabilities(
            (
                {
                    "qualified_name": "alpha.service.run",
                    "kind": "function",
                    "path": "alpha/service.py",
                },
                {
                    "qualified_name": "beta.service.run",
                    "kind": "function",
                    "path": "beta/service.py",
                },
            )
        )
        self.assertEqual([item for item in found if item.exercised_by], [])
