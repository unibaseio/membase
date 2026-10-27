"""``Membase.media``: media memory through the client, over the ``/v1/media`` routes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..client import Membase


class Media:
    """Media memory: photos, videos and audio with their own search and ask, apart from documents
    and memories. Local for now (``local=True``, or a ``membase serve`` on this machine)."""

    def __init__(self, client: Membase) -> None:
        self._c = client

    def add(
        self,
        path: str | os.PathLike | None = None,
        *,
        url: str | None = None,
        container: str | None = None,
        title: str = "",
        custom_id: str = "",
    ) -> dict:
        """Store an image, video or audio file (a ``path`` on this machine, or a public ``url``):
        captions, shots, speech and speakers are worked out before this returns."""
        body: dict[str, Any] = {"title": title, "custom_id": custom_id}
        if path is not None:
            body["path"] = str(Path(path).expanduser().resolve())
        if url is not None:
            body["url"] = url
        if container is not None:
            body["container"] = container
        return self._c._request("POST", "/v1/media", json=body)

    def list(self, *, container: str | None = None) -> dict:
        """The media the containers hold, newest first."""
        return self._c._request("GET", "/v1/media", params={"container": container} if container else None)

    def get(self, media_id: str) -> dict:
        """One item with its moments: shots and spoken lines, with times, speakers and thumbnails."""
        return self._c._request("GET", f"/v1/media/{media_id}")

    def delete(self, media_id: str, *, confirm: bool = False) -> dict:
        """Remove one item and its file; needs ``confirm=True``."""
        return self._c._request(
            "DELETE", f"/v1/media/{media_id}", params={"confirm": "true" if confirm else "false"}
        )

    def search(
        self,
        q: str,
        *,
        container: str | None = None,
        kind: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """Media matching ``q`` (any language), best first, each with the moments that matched;
        ``kind`` is image, video, audio or text."""
        body: dict[str, Any] = {"q": q}
        if container is not None:
            body["container"] = container
        if kind is not None:
            body["kind"] = kind
        if limit is not None:
            body["limit"] = limit
        return self._c._request("POST", "/v1/media/search", json=body)

    def ask(self, message: str, *, container: str | None = None) -> dict:
        """An answer from media memory alone, with the items it drew on as ``sources``."""
        body: dict[str, Any] = {"message": message}
        if container is not None:
            body["container"] = container
        return self._c._request("POST", "/v1/media/ask", json=body)
