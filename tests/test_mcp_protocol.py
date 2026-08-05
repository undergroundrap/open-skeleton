# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

import asyncio
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase, skipUnless

from open_skeleton.mcp_server import OpenSkeletonService, create_mcp_server

MCP_INSTALLED = importlib.util.find_spec("mcp") is not None


@skipUnless(MCP_INSTALLED, "official MCP SDK is an optional dependency")
class McpProtocolTests(TestCase):
    def test_initialize_list_call_invalid_input_and_shutdown(self) -> None:
        async def scenario(root: Path, state: Path) -> None:
            from mcp import Client

            service = OpenSkeletonService(root, state)
            server = create_mcp_server(service)
            async with Client(server, raise_exceptions=True) as client:
                tools = await client.list_tools()
                # The SDK has returned both a result object wrapping `.tools`
                # and a bare sequence. The wrapper is a pydantic model, and
                # iterating one of those yields (field, value) pairs rather
                # than tools, so the attribute has to be preferred explicitly
                # instead of falling through to iteration.
                wrapped = getattr(tools, "tools", None)
                listed_tools: list[Any] = list(wrapped) if wrapped is not None else list(tools)
                self.assertTrue(any(tool.name == "project_status" for tool in listed_tools))
                status = await client.call_tool("project_status", {})
                self.assertFalse(status.is_error)
                self.assertIn("not_analyzed", str(status.structured_content))
                invalid = await client.call_tool("list_claims", {"limit": 0})
                self.assertTrue(invalid.is_error)

        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repo"
            root.mkdir()
            asyncio.run(scenario(root, workspace / "state"))
