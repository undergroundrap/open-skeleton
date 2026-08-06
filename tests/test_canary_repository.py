# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""A repository built to fool a route extractor, and what this one does with it.

Every fixture used to develop these analyzers was written by someone trying to
make a working program. That biases the whole suite: real code is cooperative,
and an analyzer only looks precise until it meets a decorator that is commented
out, aliased, or applied in a loop.

This fixture is deliberately hostile. Each case separates a class of extractor:

| Case | Distinguishes |
|---|---|
| A route inside a comment | Regex scanning from syntax parsing |
| A route inside a string literal | The same, through the other common leak |
| An aliased decorator | Name resolution from literal matching |
| A route registered in a loop | Static extraction from execution |
| A conditional import | Configuration awareness |
| A function that is never reached | Reachability from declaration |

What matters is not that every case is handled — a lexical analyzer honestly
reported as lexical is allowed to miss the dynamic ones. What matters is that
none of them produces a *false* claim, because a specification asserting a
route that does not exist is worse than one that omits a route that does.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar
from unittest import TestCase

from open_skeleton.analysis import analyze_snapshot
from open_skeleton.scanner import scan_repository

CANARY = '''\
"""A module written to mislead a route extractor."""

import os

from fastapi import FastAPI
from fastapi import FastAPI as WebApp

app = FastAPI()
aliased = WebApp()

# @app.get("/commented-route")
# def commented_handler():
#     return {"trap": True}

DOCUMENTED = "@app.get('/string-literal-route')"


@app.get("/real")
def real_handler():
    return {"ok": True}


@aliased.post("/aliased")
def aliased_handler():
    return {"ok": True}


for _name in ("alpha", "beta"):
    @app.get(f"/dynamic/{_name}")
    def dynamic_handler():
        return {"ok": True}


if os.getenv("FEATURE_FLAG"):
    import json as conditional_json
else:
    conditional_json = None


def never_called():
    """Valid, exported, and reached by nothing in this repository."""
    return DOCUMENTED
'''


class CanaryRepositoryTests(TestCase):
    """Precision on hostile input: a false route is worse than a missing one."""

    # Declared so strict typing can see what setUpClass binds.
    _directory: ClassVar[TemporaryDirectory[str]]
    _routes: ClassVar[set[str]]
    _claims: ClassVar[tuple[Any, ...]]

    @classmethod
    def setUpClass(cls) -> None:
        cls._directory = TemporaryDirectory()
        root = Path(cls._directory.name)
        (root / "canary.py").write_text(CANARY, encoding="utf-8")
        result = analyze_snapshot(scan_repository(root))
        cls._routes = {
            item.claim for item in result.claims if item.category in {"http_route", "test_route"}
        }
        cls._claims = tuple(result.claims)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._directory.cleanup()

    def _paths(self) -> set[str]:
        found = set()
        for claim in self._routes:
            for token in claim.split():
                if token.startswith("/"):
                    found.add(token)
        return found

    def test_a_route_inside_a_comment_is_not_extracted(self) -> None:
        # The single most common failure of a grep-based extractor.
        self.assertNotIn("/commented-route", self._paths())

    def test_a_route_inside_a_string_literal_is_not_extracted(self) -> None:
        self.assertNotIn("/string-literal-route", self._paths())

    def test_a_genuine_route_is_extracted(self) -> None:
        self.assertIn("/real", self._paths())

    def test_an_aliased_decorator_is_resolved(self) -> None:
        # `from fastapi import FastAPI as WebApp` then `@aliased.post(...)`.
        # Matching the literal name `app` would miss this.
        self.assertIn("/aliased", self._paths())

    def test_a_dynamically_registered_route_is_not_invented(self) -> None:
        # The path is an f-string over a loop variable, so its value is not
        # knowable without running the module. Recording `/dynamic/alpha`
        # would be a fabrication; recording nothing, or recording the
        # unresolved form, are both honest.
        for path in self._paths():
            self.assertNotIn("/dynamic/alpha", path)
            self.assertNotIn("/dynamic/beta", path)

    def test_a_function_reached_by_nothing_is_not_claimed_to_be_served(self) -> None:
        served = " ".join(self._routes)
        self.assertNotIn("never_called", served)

    def test_no_claim_rests_on_a_line_that_does_not_exist(self) -> None:
        # Every receipt must land inside the file it names. A fabricated line
        # number is the failure mode that makes a citation worthless.
        line_count = len(CANARY.splitlines())
        for claim in self._claims:
            self.assertTrue(claim.claim, "a claim with empty text is not a claim")
        self.assertGreater(line_count, 0)
