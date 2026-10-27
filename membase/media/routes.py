"""The ``/v1/media`` routes, answered by a :class:`LocalBackend`'s media memory."""

from __future__ import annotations

import re
from typing import Any, Callable


def _only(body: dict, allowed: set[str]) -> dict:
    # Imported here: local/routes.py imports this module to mount ROUTES.
    from ..local.routes import _only

    return _only(body, allowed)


def _truthy(v: str | None) -> bool:
    from ..local.routes import _truthy

    return _truthy(v)


def _add(b, q, body, _m) -> Any:
    body = _only(body, {"container", "path", "url", "title", "custom_id"})
    return b.media.add(
        body.get("container"), path=body.get("path"), url=body.get("url"),
        title=body.get("title") or "", custom_id=body.get("custom_id") or "",
    )


def _search(b, q, body, _m) -> Any:
    body = _only(body, {"q", "container", "kind", "limit"})
    return b.media.search(body.get("q") or "", body.get("container"), body.get("kind"), body.get("limit"))


def _ask(b, q, body, _m) -> Any:
    body = _only(body, {"message", "container"})
    return b.media.ask(body.get("message") or "", body.get("container"))


ROUTES: list[tuple[str, re.Pattern, Callable[..., Any], int]] = [
    ("POST", re.compile(r"^/v1/media$"), _add, 202),
    ("GET", re.compile(r"^/v1/media$"), lambda b, q, body, m: b.media.list(q.get("container")), 200),
    ("POST", re.compile(r"^/v1/media/search$"), _search, 200),
    ("POST", re.compile(r"^/v1/media/ask$"), _ask, 200),
    ("GET", re.compile(r"^/v1/media/(?P<id>[^/]+)$"), lambda b, q, body, m: b.media.get(m["id"]), 200),
    ("DELETE", re.compile(r"^/v1/media/(?P<id>[^/]+)$"),
     lambda b, q, body, m: b.media.delete(m["id"], _truthy(q.get("confirm"))), 200),
]
