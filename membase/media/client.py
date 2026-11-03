"""``Membase.media``: photos and videos (``med_`` ids), over the ``/v1/media`` routes."""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from ..client import Membase


@dataclass(frozen=True)
class MediaFile:
    """A photo's or video's bytes (or one of its thumbnails), as ``Media.file`` reads them."""

    content: bytes
    content_type: str
    #: 200 for the whole file, 206 for the part a ``range`` asked for.
    status: int
    #: ``bytes <start>-<end>/<size>`` on a 206, else empty.
    content_range: str = ""


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

    def file(self, media_id: str, *, thumb: str | None = None, range: str | None = None) -> MediaFile:
        """The item's file, or the thumbnail ``thumb`` (the name after ``/thumbs/`` in a
        ``thumb_url``). ``range`` is an HTTP range (``bytes=0-1023``) and answers that part, with
        status 206 — what a video player asks for. Hosted and local alike."""
        path = f"/v1/media/{quote(media_id, safe='')}"
        path += f"/thumbs/{quote(thumb, safe='/')}" if thumb else "/file"
        r = self._c._request("GET", path, headers={"Range": range} if range else None, raw=True)
        return MediaFile(
            content=r.content,
            content_type=r.headers.get("content-type", "application/octet-stream"),
            status=r.status_code,
            content_range=r.headers.get("content-range", ""),
        )

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
        return self._c._request("POST", "/v1/media/add", json=body)

    def ask(self, message: str) -> dict:
        """Local memory only: an answer from media memory alone, with the items it drew on as
        ``sources``."""
        return self._c._request("POST", "/v1/media/ask", json={"message": message})
