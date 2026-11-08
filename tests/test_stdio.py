"""The real MCP server over stdio, driven by the official SDK client in every connect mode."""
import json
import os
import sys

import anyio
import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _params(home):
    return StdioServerParameters(command=sys.executable, args=["-m", "sozai"],
                                 env={**os.environ, "SOZAI_HOME": str(home)})


@pytest.mark.parametrize("mode", ["legacy", "auto", "2026-07-28"])
async def test_tools_over_stdio(index_path, mode):
    with anyio.fail_after(30):
        async with Client(_params(index_path.parent), mode=mode, read_timeout_seconds=20) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == {"search_assets", "get_asset", "view_assets", "fetch_asset", "update_index"}
            assert tools["search_assets"].annotations.read_only_hint is True
            assert tools["fetch_asset"].annotations.read_only_hint is False

            result = await client.call_tool("search_assets", {"query": "wooden crate", "kind": "model", "limit": 2})
            assert not result.is_error, result.content
            data = json.loads(result.content[0].text)
            assert [r["id"] for r in data["results"]] == ["polyhaven:wooden_crate_01", "polyhaven:wooden_crate_02"]
            assert result.structured_content == data

            listed = await client.call_tool("search_assets", {"query": "bricks", "kind": ["material", "decal"]})
            assert not listed.is_error

            asset = await client.call_tool("get_asset", {"asset_id": "polyhaven:wooden_crate_01"})
            assert "how_to_get" in json.loads(asset.content[0].text)

            missing = await client.call_tool("get_asset", {"asset_id": "polyhaven:nope"})
            assert missing.is_error and "search_assets" in missing.content[0].text

            bad = await client.call_tool("search_assets", {"kind": "spaceship"})
            assert bad.is_error and "kind" in bad.content[0].text
