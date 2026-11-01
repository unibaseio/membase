"""``membase mcp``: an MCP server with the hosted Membase tools, over hosted or local memory."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

from ..client import Membase
from ..errors import ConfirmationRequiredError
from ..media import mcp as media_mcp

MEMORY_PROTOCOL = """\
You are connected to the user's persistent long-term memory (Membase). It is the
authoritative record of the user's past, preferences, decisions, projects and personal facts.

1. Before answering anything about the user's own history ("remember", "last time", "my
   project", "did I", "记得", "上次", "我的", any first-person reference to past state), call
   `search_memories` (or `ask_agent` for a synthesized answer). Do not use Conversation Search
   or Web Search for this: they are not the user's memory.
2. When the user states something durable (a decision, a preference, a personal fact, a
   milestone, their role), call `add_memory` without being asked. Skip pleasantries; the bar
   is "would they want this recalled in six months?". Raw material (a file, a page, notes)
   goes to `add_document`.
3. If memory has nothing relevant, say so ("I don't have that in memory — want me to add
   it?"). Do not guess and do not pretend to remember.
4. When memory conflicts with what you would otherwise say, memory wins.
"""


def _tool_specs(client: Callable[[], Membase]) -> list[tuple[str, str, Callable[..., Any]]]:

    def search_memories(q: str, limit: int | None = None, mode: str = "full") -> dict:
        return client().search(q, limit=limit, mode=mode)

    def get_profile(q: str | None = None) -> dict:
        return client().profile(q)

    def list_documents(kind: str | None = None, limit: int | None = None) -> dict:
        return client().documents.list(kind=kind, limit=limit)

    def memory_rules() -> dict:
        return client().rules()

    def add_memory(content: str, title: str = "") -> dict:
        return client().memories.add(content, title=title)

    def add_document(
        content: str | None = None,
        url: str | None = None,
        title: str = "",
        custom_id: str = "",
    ) -> dict:
        return client().add(content, url=url, title=title, custom_id=custom_id)

    def delete_document(document_id: str, confirm: bool = False) -> dict:
        try:
            return client().documents.delete(document_id, confirm=confirm)
        except ConfirmationRequiredError as e:
            return e.body

    def forget_memory(memory_id: str, confirm: bool = False) -> dict:
        try:
            return client().memories.forget(memory_id, confirm=confirm)
        except ConfirmationRequiredError as e:
            return e.body

    def ask_agent(message: str) -> dict:
        return client().ask(message)

    return [
        ("search_memories",
         "Passages the user's memory holds on `q`, most relevant first, each naming the Memory it "
         "came from (`memory_name`); a fact carries its `mem_` id. `limit` is the total number of "
         "passages; `mode=\"fast\"` is one round with no model call. Retrieval, not an answer.",
         search_memories),
        ("get_profile",
         "Who the user is: `static` standing facts, `dynamic` the most recently changed memories, "
         "and `results` for `q` when given.", get_profile),
        ("list_documents",
         "The material the user's memory was given, newest first; `kind` is `file` or `note`.",
         list_documents),
        ("memory_rules", "The user's standing rules for how their memory is used.", memory_rules),
        ("add_memory", "Save one durable fact or a short exchange, in the user's words.", add_memory),
        ("add_document",
         "Give the user's memory raw material to learn: `content` text or a public `url`. The same "
         "`custom_id` again is a no-op.", add_document),
        ("delete_document",
         "Remove a document and what was learned from it. Without `confirm=true` nothing is removed "
         "and the answer names what would be.", delete_document),
        ("forget_memory",
         "Forget one memory by its `mem_` id. Without `confirm=true` nothing is forgotten and the "
         "answer names what would be.", forget_memory),
        ("ask_agent", "An answer to `message` composed from the user's memory.", ask_agent),
    ]


def build_server(client: Membase | Callable[[], Membase], private_key: str | None = None) -> Any:
    """The FastMCP server."""
    from ..requirements import require_mcp

    require_mcp()
    from mcp.server.fastmcp import FastMCP

    get = client if callable(client) and not isinstance(client, Membase) else (lambda: client)
    media = media_mcp.enabled(client)
    mcp = FastMCP(name="membase", instructions=MEMORY_PROTOCOL + (media_mcp.PROTOCOL if media else ""))

    for name, description, fn in _tool_specs(get) + (media_mcp.tool_specs(get) if media else []):
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
