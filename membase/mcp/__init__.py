"""``membase mcp``: an MCP server with the hosted Membase tools, over hosted or local memory."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

from ..client import Membase
from ..media import mcp as media_mcp

MEMORY_PROTOCOL = """\
You are connected to the user's persistent long-term memory (Membase). It is the
authoritative record of the user's past, preferences, decisions, projects and personal facts.

1. Before answering anything about the user's own history ("remember", "last time", "my
   project", "did I", "记得", "上次", "我的", any first-person reference to past state), call
   `search_memories` (or `ask_memory` for one written answer). Do not use Conversation Search
   or Web Search for this: they are not the user's memory. Call `list_profile_facts` once near
   the start of a conversation to learn who the user is.
2. When the user states something durable (a decision, a preference, a milestone), call
   `add_memory` without being asked. Skip pleasantries; the bar is "would they want this
   recalled in six months?". A standing fact about the user themselves (their name, role, a
   lasting preference) goes to `add_profile_fact`. Raw material (a file, a page, notes) goes to
   `add_document`.
3. If memory has nothing relevant, say so ("I don't have that in memory — want me to add
   it?"). Do not guess and do not pretend to remember. When you use something you found, say
   which memory it came from (`memory_name`).
4. When memory conflicts with what you would otherwise say, memory wins. Never forget or
   delete anything unless the user asks.
"""

_LIMIT = "`limit` is how many, newest first: 1-1000, default 100."
_CONFIRM = "Without `confirm=true` nothing changes and the call fails with `confirmation_required`."


def _tool_specs(client: Callable[[], Membase]) -> list[tuple[str, str, Callable[..., Any]]]:

    def get_capabilities() -> dict:
        return client().capabilities()

    def search_memories(q: str, limit: int | None = None, mode: str = "full") -> dict:
        return client().search(q, limit=limit, mode=mode)

    def ask_memory(message: str) -> dict:
        return client().ask(message)

    def list_memories(limit: int | None = None) -> dict:
        return client().memories.list(limit=limit)

    def get_memory(memory_id: str) -> dict:
        return client().memories.get(memory_id)

    def add_memory(content: str, title: str = "") -> dict:
        return client().memories.add(content, title=title)

    def forget_memory(memory_id: str, confirm: bool = False) -> dict:
        return client().memories.forget(memory_id, confirm=confirm)

    def list_profile_facts(limit: int | None = None) -> dict:
        return client().profile_facts.list(limit=limit)

    def add_profile_fact(content: str) -> dict:
        return client().profile_facts.add(content)

    def forget_profile_fact(fact_id: str, confirm: bool = False) -> dict:
        return client().profile_facts.forget(fact_id, confirm=confirm)

    def list_documents(kind: str | None = None, limit: int | None = None) -> dict:
        return client().documents.list(kind=kind, limit=limit)

    def get_document(document_id: str) -> dict:
        return client().documents.get(document_id)

    def add_document(
        content: str | None = None,
        url: str | None = None,
        title: str = "",
        custom_id: str = "",
    ) -> dict:
        return client().add(content, url=url, title=title, custom_id=custom_id)

    def delete_document(document_id: str, confirm: bool = False) -> dict:
        return client().documents.delete(document_id, confirm=confirm)

    return [
        ("get_capabilities",
         "What this connection can do: the actions it holds, the Memories it may use (by name) and "
         "the user's standing rules for using them. Read-only.", get_capabilities),
        ("search_memories",
         "Passages the user's memory holds on `q`, most relevant first, each naming the Memory it "
         "came from (`memory_name`); a fact carries its `mem_` id. `limit` is the total number of "
         "passages (1-50); `mode=\"fast\"` is one round with no model call. Retrieval, not an answer.",
         search_memories),
        ("ask_memory",
         "One answer to `message`, written from what the user's memory holds, with the Memory it "
         "came from (`citations`). The passages themselves are not returned.", ask_memory),
        ("list_memories", f"The newest facts the user's memory learned, newest first. {_LIMIT}",
         list_memories),
        ("get_memory", "One learned fact by its `mem_` id (from search_memories or list_memories).",
         get_memory),
        ("add_memory",
         "Save one durable fact or a short exchange, in the user's words. It is written as a note "
         "(`doc_` id) and learned; a standing fact about the user themselves goes to "
         "add_profile_fact.", add_memory),
        ("forget_memory",
         f"Forget one learned fact by its `mem_` id; the material it came from stays. {_CONFIRM}",
         forget_memory),
        ("list_profile_facts",
         f"Who the user is: standing statements (name, role, lasting preferences), newest first, "
         f"each with its `pf_` id and `source_kind`. {_LIMIT}", list_profile_facts),
        ("add_profile_fact",
         "Keep one standing statement about the user themselves, in their words. The same one again "
         "keeps the one there (`created: false`).", add_profile_fact),
        ("forget_profile_fact",
         f"Forget one profile statement by its `pf_` id. To correct one, add the new statement and "
         f"forget the old. {_CONFIRM}", forget_profile_fact),
        ("list_documents",
         f"The material the user's memory was given, newest first; `kind` is `file` or `note`. {_LIMIT}",
         list_documents),
        ("get_document", "One document by its `doc_` id, with where its learning stands.", get_document),
        ("add_document",
         "Give the user's memory raw material to learn: `content` text or a public `url`. The same "
         "`custom_id` again is a no-op.", add_document),
        ("delete_document",
         f"Remove a document by its `doc_` id and what was learned from it. {_CONFIRM}", delete_document),
    ]


def build_server(client: Membase | Callable[[], Membase], private_key: str | None = None) -> Any:
    """The FastMCP server."""
    from ..requirements import require_mcp

    require_mcp()
    from mcp.server.fastmcp import FastMCP

    get = client if callable(client) and not isinstance(client, Membase) else (lambda: client)
    media, local_media = media_mcp.enabled(client), media_mcp.local_only(client)
    protocol = media_mcp.LOCAL_PROTOCOL if local_media else media_mcp.PROTOCOL if media else ""
    mcp = FastMCP(name="membase", instructions=MEMORY_PROTOCOL + protocol)

    specs = _tool_specs(get) + (media_mcp.tool_specs(get, local=local_media) if media else [])
    for name, description, fn in specs:
# The local engine uses async_to_sync, which refuses the event loop's thread.
        async def tool(*args: Any, __fn: Callable[..., Any] = fn, **kwargs: Any) -> Any:
            return await asyncio.to_thread(__fn, *args, **kwargs)

        tool.__signature__ = __import__("inspect").signature(fn)  # type: ignore[attr-defined]
        tool.__name__ = name
        mcp.add_tool(tool, name=name, description=description)

    key = private_key if private_key is not None else (os.environ.get("MEMBASE_PRIVATE_KEY") or None)
    if key:
        from .automem_tools import register_automem_tools

        register_automem_tools(mcp, key)
    return mcp


def run(client: Membase, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765) -> None:
    server = build_server(client)
    if transport in ("sse", "streamable-http"):
        server.settings.host = host
        server.settings.port = port
    server.run(transport=transport)
