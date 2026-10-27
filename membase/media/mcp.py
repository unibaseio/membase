"""The media tools of ``membase mcp``, offered for local memory with the media extra installed."""

from __future__ import annotations

from typing import Any, Callable

from ..client import Membase

PROTOCOL = """
5. Photos, videos and audio recordings live in media memory, apart from the rest: look there
   with `search_media` (or `ask_media` for an answer), and store a file the user hands you
   with `add_media`.
"""


def enabled(client: Membase | Callable[[], Membase]) -> bool:
    """Media tools only for local memory with the media extra installed, so the hosted tool table
    stays as it is."""
    from .requirements import media_unavailable

    if not isinstance(client, Membase):
        return False
    local = getattr(client._http._transport, "backend", None) is not None
    return local and media_unavailable() is None


def tool_specs(client: Callable[[], Membase]) -> list[tuple[str, str, Callable[..., Any]]]:
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
