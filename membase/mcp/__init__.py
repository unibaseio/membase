"""``membase mcp``: an MCP server with the hosted Membase tools, over hosted or local memory."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

from ..client import Membase

MEMORY_PROTOCOL = """\
You are connected to the user's persistent long-term memory (Membase). It is the
authoritative record of the user's past, preferences, decisions, projects and personal facts.

1. Before answering anything about the user's own history ("remember", "last time", "my
   project", "did I", "记得", "上次", "我的", any first-person reference to past state), call
   `search_memories` (or `ask_agent` for a synthesized answer). Do not use Conversation Search
   or Web Search for this: they are not the user's memory.
2. When the user states something durable (a decision, a preference, a personal fact, a
   milestone, their role), call `add_memory` without being asked. `static=true` is for a
   standing fact about the user. Skip pleasantries; the bar is "would they want this recalled
   in six months?". Raw material (a file, a page, notes) goes to `add_document`.
3. If memory has nothing relevant, say so ("I don't have that in memory — want me to add
   it?"). Do not guess and do not pretend to remember.
4. When memory conflicts with what you would otherwise say, memory wins.
"""


MEDIA_PROTOCOL = """
5. Photos, videos and audio recordings live in media memory, apart from the rest: look there
   with `search_media` (or `ask_media` for an answer), and store a file the user hands you
   with `add_media`.
"""


def _media_tool_specs(client: Callable[[], Membase]) -> list[tuple[str, str, Callable[..., Any]]]:
    def add_media(path: str | None = None, url: str | None = None, container: str | None = None,
                  title: str = "", custom_id: str = "") -> dict:
        return client().media.add(path, url=url, container=container, title=title, custom_id=custom_id)

    def list_media(container: str | None = None) -> dict:
        return client().media.list(container=container)

    def get_media(media_id: str) -> dict:
        return client().media.get(media_id)

    def search_media(q: str, container: str | None = None, kind: str | None = None,
                     limit: int | None = None) -> dict:
        return client().media.search(q, container=container, kind=kind, limit=limit)

    def ask_media(message: str, container: str | None = None) -> dict:
        return client().media.ask(message, container=container)

    def delete_media(media_id: str, confirm: bool = False) -> dict:
        return client().media.delete(media_id, confirm=confirm)

    return [
        ("add_media",
         "Store an image, video or audio file: a `path` on this machine or a public `url`. Shots, "
         "captions, speech and speakers are worked out before it returns (minutes for a long video).",
         add_media),
        ("list_media", "The photos, videos and recordings in media memory, newest first.", list_media),
        ("get_media", "One media item with its shots and spoken lines (times, speakers, thumbnails).",
         get_media),
        ("search_media",
         "Photos, videos and recordings matching `q` (any language), best first, each with the "
         "moments that matched. `kind` is image, video, audio or text.", search_media),
        ("ask_media", "An answer from media memory alone, with the items it drew on.", ask_media),
        ("delete_media",
         "Remove a media item. Without `confirm=true` this only returns what would be removed.",
         delete_media),
    ]


def _media_enabled(client: Membase | Callable[[], Membase]) -> bool:
    """Media tools only for local memory with the media extra installed."""
    from ..requirements import media_unavailable

    if not isinstance(client, Membase):
        return False
    local = getattr(client._http._transport, "backend", None) is not None  # noqa: SLF001
    return local and media_unavailable() is None


def _tool_specs(client: Callable[[], Membase]) -> list[tuple[str, str, Callable[..., Any]]]:

    def list_containers() -> dict:
        return client().containers.list()

    def search_memories(q: str, container: str | None = None, limit: int | None = None) -> dict:
        return client().search(q, container=container, limit=limit)

    def get_profile(q: str | None = None) -> dict:
        return client().profile(q)

    def list_documents(container: str | None = None) -> dict:
        return client().documents.list(container=container)

    def memory_rules() -> dict:
        return client().rules()

    def add_memory(content: str, container: str | None = None, static: bool = False, title: str = "") -> dict:
        return client().memories.add(content, container=container, static=static, title=title)

    def add_document(
        container: str,
        content: str | None = None,
        url: str | None = None,
        title: str = "",
        custom_id: str = "",
    ) -> dict:
        return client().add(content, container=container, url=url, title=title, custom_id=custom_id)

    def delete_document(document_id: str, confirm: bool = False) -> dict:
        return client().documents.delete(document_id, confirm=confirm)

    def forget_memory(memory_id: str, container: str | None = None, confirm: bool = False) -> dict:
        return client().memories.forget(memory_id, container=container, confirm=confirm)

    def ask_agent(message: str) -> dict:
        return client().ask(message)

    return [
        ("list_containers", "The memory containers you may use.", list_containers),
        ("search_memories",
         "Passages the user's memory holds on `q`, most relevant first, each naming its container. "
         "Omit `container` to search every container. `limit` is the total number of passages. "
         "Retrieval, not an answer.", search_memories),
        ("get_profile",
         "Who the user is: `static` standing facts, `dynamic` the most recently changed memories, "
         "and `results` for `q` when given.", get_profile),
        ("list_documents", "The documents the containers have read, newest first.", list_documents),
        ("memory_rules", "The user's standing rules for how their memory is used.", memory_rules),
        ("add_memory",
         "Save one durable fact or a short exchange. `static=true` records a standing fact about "
         "the user in their profile; otherwise it is remembered in the container.", add_memory),
        ("add_document",
         "Give a container raw material to learn: `content` text or a public `url`. The same "
         "`custom_id` again is a no-op.", add_document),
        ("delete_document",
         "Remove a document. Without `confirm=true` this only returns what would be removed.",
         delete_document),
        ("forget_memory",
         "Forget one memory by id. Without `confirm=true` this only returns what would be forgotten.",
         forget_memory),
        ("ask_agent", "An answer to `message` composed from the user's memory.", ask_agent),
    ]


def build_server(client: Membase | Callable[[], Membase], private_key: str | None = None) -> Any:
    """The FastMCP server."""
    from ..requirements import require_mcp

    require_mcp()
    from mcp.server.fastmcp import FastMCP

    get = client if callable(client) and not isinstance(client, Membase) else (lambda: client)
    media = _media_enabled(client)
    mcp = FastMCP(name="membase", instructions=MEMORY_PROTOCOL + (MEDIA_PROTOCOL if media else ""))

    for name, description, fn in _tool_specs(get) + (_media_tool_specs(get) if media else []):
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
