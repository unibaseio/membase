"""The media tools of ``membase mcp``: the hosted ones (list, get, search, delete) over hosted
memory or local memory with the media extra installed, plus storing a file and asking (local
only)."""

from __future__ import annotations

from typing import Any, Callable

from ..client import Membase

PROTOCOL = """
5. Photos and videos live in media memory, apart from the rest: look there with
   `search_media`.
"""
LOCAL_PROTOCOL = """
5. Photos, videos and audio recordings live in media memory, apart from the rest: look there
   with `search_media` (or `ask_media` for an answer), and store a file the user hands you
   with `add_media`.
"""


def _local(client: Membase | Callable[[], Membase]) -> bool:
    return isinstance(client, Membase) and getattr(client._http._transport, "backend", None) is not None


def enabled(client: Membase | Callable[[], Membase]) -> bool:
    """Media tools over hosted memory, and over local memory with the media extra installed."""
    from .requirements import media_unavailable

    return not _local(client) or media_unavailable() is None


def local_only(client: Membase | Callable[[], Membase]) -> bool:
    """Storing a file and asking: local memory with the media extra installed."""
    return enabled(client) and _local(client)


def tool_specs(
    client: Callable[[], Membase], *, local: bool = False
) -> list[tuple[str, str, Callable[..., Any]]]:
    def add_media(path: str | None = None, url: str | None = None, title: str = "", custom_id: str = "") -> dict:
        return client().media.add(path, url=url, title=title, custom_id=custom_id)

    def list_media(limit: int | None = None) -> dict:
        return client().media.list(limit=limit)

    def get_media(media_id: str) -> dict:
        return client().media.get(media_id)

    def search_media(q: str, kind: str | None = None, limit: int | None = None) -> dict:
        return client().media.search(q, kind=kind, limit=limit)

    def ask_media(message: str) -> dict:
        return client().media.ask(message)

    def delete_media(media_id: str, confirm: bool = False) -> dict:
        return client().media.delete(media_id, confirm=confirm)

    hosted = [
        ("list_media", "The photos and videos in media memory, newest first, each with a one-sentence "
         "summary.", list_media),
        ("get_media", "One media item with its shots and spoken lines (times, speakers, thumbnails).",
         get_media),
        ("search_media",
         "Photos and videos matching `q` (any language), best first, each with the moments that "
         "matched. `kind` is image or video" + (" (locally also audio or text)." if local else "."),
         search_media),
        ("delete_media",
         "Forget a media item by its `med_` id and remove its file. Without `confirm=true` nothing "
         "changes and the call fails with `confirmation_required`.", delete_media),
    ]
    if not local:
        return hosted
    return [
        ("add_media",
         "Store an image, video or audio file: a `path` on this machine or a public `url`. Shots, "
         "captions, speech and speakers are worked out before it returns (minutes for a long video).",
         add_media),
        *hosted,
        ("ask_media", "An answer from media memory alone, with the items it drew on.", ask_media),
    ]
