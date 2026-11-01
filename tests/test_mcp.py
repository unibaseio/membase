"""The MCP server: the hosted tool table, over a real stdio session against local memory."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

pytest.importorskip("mcp", reason="needs the mcp extra")
pytest.importorskip("membase_core", reason="needs the local extra")

PROTOCOL_TOOLS = {
    "search_memories", "get_profile", "list_documents", "memory_rules",
    "add_memory", "add_document", "delete_document", "forget_memory", "ask_agent",
}
MEDIA_TOOLS = {"add_media", "list_media", "get_media", "search_media", "ask_media", "delete_media"}


def _local_tools() -> set[str]:
    from membase.media.requirements import media_unavailable

    return PROTOCOL_TOOLS | (MEDIA_TOOLS if media_unavailable() is None else set())


async def _drive(store, env):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "membase", "--local", "--store", str(store), "mcp"], env=env,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = (await session.list_tools()).tools
        found = await session.call_tool("search_memories", {"q": "tea", "mode": "fast"})
        documents = await session.call_tool("list_documents", {"kind": "note"})
        forget = await session.call_tool("forget_memory", {"memory_id": "mem_bm9uZQ"})
        return listed, found, documents, forget


def test_stdio_server_serves_the_protocol_tools(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("MEMBASE_PRIVATE_KEY", "MEMBASE_API_KEY")}
    listed, found, documents, forget = asyncio.run(_drive(tmp_path / "store", env))
    assert {t.name for t in listed} == _local_tools()
    for tool in listed:
        assert "container" not in tool.inputSchema.get("properties", {}), tool.name
    assert json.loads(found.content[0].text) == {"items": [], "partial": []}
    assert json.loads(documents.content[0].text) == {"items": [], "has_more": False, "partial": []}
    assert forget.isError and "memory not found" in forget.content[0].text


def test_automem_tools_appear_with_a_wallet_key(tmp_path):
    from membase import Membase
    from membase.mcp import build_server

    m = Membase(local=tmp_path / "s")
    server = build_server(m, private_key="0x" + "11" * 32)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == _local_tools() | {"automem_save", "automem_list", "automem_fetch", "automem_delete"}
    m.close()
