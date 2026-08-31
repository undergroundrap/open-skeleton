# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

"""Conservative normalization for request targets that can denote local routes."""

from __future__ import annotations

from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})  # noqa: S104


def local_request_path(target: str) -> str | None:
    """Return the path when a request target is relative or explicitly loopback.

    An arbitrary absolute URL can spell the same path as a route in the
    repository while targeting a different service, so it is deliberately not
    reduced. Query and fragment components do not participate in HTTP route
    registration and are removed.
    """

    if target.startswith("/") and not target.startswith("//"):
        return urlsplit(target).path or "/"
    parsed = urlsplit(target)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return None
    if (parsed.hostname or "").casefold() not in LOOPBACK_HOSTS:
        return None
    return parsed.path or "/"
