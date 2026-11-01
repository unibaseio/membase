"""Membase on this machine: the hosted memory API answered by the unibaseio-membase-core engine."""

from __future__ import annotations

import json
import os
from urllib.parse import parse_qsl

import httpx

from .backend import DEFAULT_CONTAINER, LocalBackend, LocalError
from .routes import Raw, dispatch

__all__ = ["DEFAULT_CONTAINER", "LocalBackend", "LocalError", "LocalTransport", "dispatch"]


class LocalTransport(httpx.BaseTransport):
    """An httpx transport that answers the ``/v1`` routes from a local engine, in process."""

    BASE_URL = "http://membase.local"

    def __init__(self, root: str | os.PathLike | None = None, backend: LocalBackend | None = None) -> None:
        self.backend = backend or LocalBackend(root)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raw = request.read()
        body = json.loads(raw) if raw else None
        query = dict(parse_qsl(request.url.query.decode() if isinstance(request.url.query, bytes)
                               else str(request.url.query)))
        status, payload = dispatch(self.backend, request.method, request.url.path, query, body,
                                   dict(request.headers))
        if isinstance(payload, Raw):
            return httpx.Response(status, content=payload.body, request=request,
                                  headers={"Content-Type": payload.content_type, **payload.headers})
        return httpx.Response(status, json=payload, request=request)

    def close(self) -> None:
        self.backend.close()
