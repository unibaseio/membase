"""The ``/v1/media`` routes, answered by a :class:`LocalBackend`'s media memory: the hosted media
routes (list, get, search, delete, the file and its thumbnails) plus two that only local memory has,
``POST /v1/media`` (store a file) and ``POST /v1/media/ask``."""

from __future__ import annotations

import re
from typing import Any, Callable

from ..local.backend import LocalError

KINDS = ("image", "video", "audio", "text")


def _r():
    from ..local import routes

    return routes


def _item(b, public_id: str) -> tuple[str, str]:
    """The media id in its container, for a public id this store holds; 404 otherwise."""
    from ..local import ids

    cid, raw = ids.decode("media", public_id)
    if cid not in b._ids():
        raise LocalError(404, "not_found", "media not found", {"id": public_id})
    row = _r()._named(public_id, lambda: b.media.get(raw))
    if row["container"] != cid:
        raise LocalError(404, "not_found", "media not found", {"id": public_id})
    return cid, raw


def _add(b, q, body, _m) -> Any:
    r = _r()
    body = r._only(body, {"path", "url", "title", "custom_id"})
    if bool(body.get("path")) == bool(body.get("url")):
        raise LocalError(400, "validation", "give exactly one of path and url")
    row = b.media.add(
        b.write_target(), path=body.get("path") or None, url=body.get("url") or None,
        title=r._text(body, "title", max_length=200), custom_id=r._text(body, "custom_id", max_length=200),
    )
    return r._media_out(row)


def _list(b, q, body, _m) -> Any:
    r = _r()
    size = r._list_limit(q)
    return r._list([r._media_out(row) for row in b.media.list()["media"]], size)


def _search(b, q, body, _m) -> Any:
    r = _r()
    body = r._only(body, {"q", "kind", "limit"}, required=("q",))
    kind = r._choice(body.get("kind"), ["body", "kind"], KINDS)
    limit = r._int_or_none(body.get("limit"), ["body", "limit"], 1, 50)
    out = b.media.search(r._text(body, "q"), None, kind, limit)
    return {"items": [r._media_out(row) for row in out["results"]], "partial": r._partial(out["containers"])}


def _ask(b, q, body, _m) -> Any:
    r = _r()
    body = r._only(body, {"message"})
    out = b.media.ask(r._text(body, "message"))
    names = r._names(b)
    return {"answer": out["answer"], "sources": [r._media_out(row) for row in out["sources"]],
            "memory_name": names.get(out.get("container") or "", "")}


def _get(b, q, body, m) -> Any:
    _, raw = _item(b, m["id"])
    return _r()._media_out(b.media.get(raw))


def _delete(b, q, body, m) -> Any:
    r = _r()
    _, raw = _item(b, m["id"])
    out = r._named(m["id"], lambda: b.media.delete(raw, r._truthy(q.get("confirm"))))
    return r._confirm_or(out, m["id"], "deleted")


def _file(b, q, body, m) -> Any:
    r = _r()
    _, raw = _item(b, m["id"])
    path = r._named(m["id"], lambda: b.media.file(raw, m.get("thumb")))
    return r.file_answer(path, (m.get("headers") or {}).get("range", ""))


ROUTES: list[tuple[str, re.Pattern, Callable[..., Any], int]] = [
    ("POST", re.compile(r"^/v1/media$"), _add, 202),
    ("GET", re.compile(r"^/v1/media$"), _list, 200),
    ("POST", re.compile(r"^/v1/media/search$"), _search, 200),
    ("POST", re.compile(r"^/v1/media/ask$"), _ask, 200),
    ("GET", re.compile(r"^/v1/media/(?P<id>[^/]+)$"), _get, 200),
    ("DELETE", re.compile(r"^/v1/media/(?P<id>[^/]+)$"), _delete, 200),
    ("GET", re.compile(r"^/v1/media/(?P<id>[^/]+)/file$"), _file, 200),
    ("GET", re.compile(r"^/v1/media/(?P<id>[^/]+)/thumbs/(?P<thumb>.+)$"), _file, 200),
]
