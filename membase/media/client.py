"""``Membase.media``: photos and videos (``med_`` ids), over the ``/v1/media`` routes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from ..client import Membase


class Media:
    """Media memory: the photos and videos the media Memories in reach hold, apart from documents
    and memories. Hosted and local alike list, read, search and delete; local memory (``local=True``
    or ``membase serve``) also stores files (``add``, audio too) and answers from them (``ask``)."""

    def __init__(self, client: Membase) -> None:
        self._c = client

    def list(self, *, limit: int | None = None) -> dict:
        """The photos and videos, newest first (``limit`` 1-1000, default 100), each with its
        one-sentence ``summary``, ``file_url`` and ``thumb_url``: ``{items, has_more, partial}``."""
        return self._c._request("GET", "/v1/media", params={"limit": limit} if limit is not None else None)

    def get(self, media_id: str) -> dict:
        """One item with its ``moments``: a video's shots and spoken lines, with times, speakers and
        thumbnails."""
        return self._c._request("GET", f"/v1/media/{quote(media_id, safe='')}")

    def search(self, q: str, *, kind: str | None = None, limit: int | None = None) -> dict:
        """Items matching what was seen or said in ``q`` (any language), best first, each with the
        moments that matched: ``{items, partial}``. ``kind`` is ``image`` or ``video``."""
        body: dict[str, Any] = {"q": q}
        if kind is not None:
            body["kind"] = kind
        if limit is not None:
            body["limit"] = limit
        return self._c._request("POST", "/v1/media/search", json=body)

    def delete(self, media_id: str, *, confirm: bool = False) -> dict:
        """Forget one item and remove its file. Needs Full access and ``confirm=True``; without it
        nothing changes and :class:`~membase.ConfirmationRequiredError` is raised."""
        return self._c._request(
            "POST", f"/v1/media/{quote(media_id, safe='')}/delete", params={"confirm": "true" if confirm else "false"}
        )

    def add(
        self,
        path: str | os.PathLike | None = None,
        *,
        url: str | None = None,
        title: str = "",
        custom_id: str = "",
    ) -> dict:
        """Local memory only: store an image, video or audio file (a ``path`` on the machine running
        the engine, or a public ``url``) in the write target. Captions, shots, speech and speakers
        are worked out before this returns; the answer is the stored item."""
        body: dict[str, Any] = {"title": title, "custom_id": custom_id}
        if path is not None:
            body["path"] = str(Path(path).expanduser().resolve())
        if url is not None:
            body["url"] = url
        return self._c._request("POST", "/v1/media", json=body)

    def ask(self, message: str) -> dict:
        """Local memory only: an answer from media memory alone, with the items it drew on as
        ``sources``."""
        return self._c._request("POST", "/v1/media/ask", json={"message": message})
