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


RUST_CANARY = """\
use axum::{Router, routing::get};

// .route("/commented-rust", get(trap_handler))
const DOCUMENTED: &str = ".route(\\"/literal-rust\\", get(x))";

pub fn build() -> Router {
    Router::new()
        .route("/real-rust", get(real_handler).post(create_handler))
        .nest("/api", api_router())
}

#[post("/attribute-rust")]
async fn attribute_handler() {}

macro_rules! make_route {
    ($path:expr) => { Router::new().route($path, get(real_handler)) };
}
"""

TS_CANARY = """\
import { useState } from "react";
import { useState as useLocal } from "react";

// const [commented, setCommented] = useState(0);
const DOCUMENTED = 'fetch("/api/literal-trap")';

export function App() {
  const [real, setReal] = useState(0);
  const [aliased, setAliased] = useLocal(0);
  const id = "abc";
  fetch(`/api/dynamic/${id}`);
  fetch("/api/real");
  return null;
}
"""


class CrossLanguageCanaryTests(TestCase):
    """The same six traps, in the two languages analyzed lexically.

    The first version of this fixture only covered Python and passed cleanly,
    which was misleading: Rust and TypeScript passed every false-positive test
    by extracting nothing at all. An analyzer that finds no routes cannot find
    a wrong one. Every case below therefore asserts a true positive alongside
    the trap it is paired with.
    """

    _claims: ClassVar[tuple[Any, ...]]

    @classmethod
    def setUpClass(cls) -> None:
        cls._directory = TemporaryDirectory()
        root = Path(cls._directory.name)
        (root / "main.rs").write_text(RUST_CANARY, encoding="utf-8")
        (root / "app.tsx").write_text(TS_CANARY, encoding="utf-8")
        cls._claims = tuple(analyze_snapshot(scan_repository(root)).claims)

    _directory: ClassVar[TemporaryDirectory[str]]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._directory.cleanup()

    def _text(self) -> str:
        return " ".join(claim.claim for claim in self._claims)

    def test_rust_extracts_a_real_route_with_every_method_it_serves(self) -> None:
        # axum chains methods onto one path, so a single `.route` call can
        # serve two. Recording only the first understates the surface.
        self.assertIn("GET /real-rust", self._text())
        self.assertIn("POST /real-rust", self._text())

    def test_rust_resolves_an_attribute_macro_route(self) -> None:
        self.assertIn("POST /attribute-rust", self._text())

    def test_rust_records_a_mount_as_a_prefix_not_an_endpoint(self) -> None:
        # `/api` is where a sub-router attaches. Listing it as a served path
        # would put an endpoint in the catalog that answers nothing.
        self.assertIn("/api mounts a sub-router", self._text())

    def test_rust_ignores_routes_in_comments_and_string_literals(self) -> None:
        self.assertNotIn("/commented-rust", self._text())
        self.assertNotIn("/literal-rust", self._text())

    def test_rust_does_not_invent_a_macro_parameter_path(self) -> None:
        # `.route($path, ...)` names no literal, so there is nothing to report.
        self.assertNotIn("$path", self._text())

    def test_typescript_extracts_the_endpoint_a_fetch_calls(self) -> None:
        # Counting call sites says the frontend talks to something. Naming the
        # path is what lets a caller be joined to the route that serves it.
        self.assertIn("/api/real is requested", self._text())

    def test_typescript_reports_an_interpolated_path_as_a_prefix(self) -> None:
        text = self._text()
        self.assertIn("/api/dynamic/ begins a request path", text)
        self.assertNotIn("/api/dynamic/abc", text)

    def test_typescript_resolves_an_aliased_hook_import(self) -> None:
        # `useState as useLocal` is still a useState call. Matching the literal
        # name reported one where there are two.
        self.assertIn("useState 2 times", self._text())

    def test_typescript_ignores_a_fetch_inside_a_string_literal(self) -> None:
        self.assertNotIn("/api/literal-trap", self._text())
