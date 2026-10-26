"""The ``/v1`` routes of the agent protocol, answered by a :class:`LocalBackend`."""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import unquote

from .backend import LocalBackend, LocalError

_Body = dict[str, Any]
_Query = dict[str, str]


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes"}


def _only(body: _Body, allowed: set[str]) -> _Body:
    unknown = set(body) - allowed
    if unknown:
        raise LocalError(400, "bad_request", f"unknown fields: {', '.join(sorted(unknown))}")
    return body


def _search(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    body = _only(body, {"q", "container", "limit"})
    return b.search(body.get("q") or "", body.get("container"), body.get("limit"))


def _add_document(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    body = _only(body, {"container", "content", "url", "title", "metadata", "custom_id"})
    return b.add_document(
        body.get("container"), content=body.get("content"), url=body.get("url"),
        title=body.get("title") or "", metadata=body.get("metadata"),
        custom_id=body.get("custom_id") or "",
    )


def _add_memory(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    body = _only(body, {"content", "container", "static", "title"})
    return b.add_memory(
        body.get("content") or "", body.get("container"), bool(body.get("static")),
        body.get("title") or "",
    )


def _ask(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    body = _only(body, {"message", "model"})
    return b.ask(body.get("message") or "", model=body.get("model"))


def _add_media(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    body = _only(body, {"container", "path", "url", "title", "custom_id"})
    return b.media.add(
        body.get("container"), path=body.get("path"), url=body.get("url"),
        title=body.get("title") or "", custom_id=body.get("custom_id") or "",
    )


def _search_media(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    body = _only(body, {"q", "container", "kind", "limit"})
    return b.media.search(body.get("q") or "", body.get("container"), body.get("kind"), body.get("limit"))


def _ask_media(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    body = _only(body, {"message", "container"})
    return b.media.ask(body.get("message") or "", body.get("container"))


ROUTES: list[tuple[str, re.Pattern, Callable[..., Any], int]] = [
    ("GET", re.compile(r"^/v1/containers$"), lambda b, q, body, m: b.containers(), 200),
    ("POST", re.compile(r"^/v1/search$"), _search, 200),
    ("GET", re.compile(r"^/v1/profile$"), lambda b, q, body, m: b.profile(q.get("q")), 200),
    ("GET", re.compile(r"^/v1/rules$"), lambda b, q, body, m: b.rules(), 200),
    ("GET", re.compile(r"^/v1/documents$"), lambda b, q, body, m: b.documents(q.get("container")), 200),
    ("POST", re.compile(r"^/v1/documents$"), _add_document, 202),
    ("GET", re.compile(r"^/v1/documents/(?P<id>[^/]+)$"), lambda b, q, body, m: b.document(m["id"]), 200),
    ("DELETE", re.compile(r"^/v1/documents/(?P<id>[^/]+)$"),
     lambda b, q, body, m: b.delete_document(m["id"], _truthy(q.get("confirm"))), 200),
    ("POST", re.compile(r"^/v1/memories$"), _add_memory, 200),
    ("DELETE", re.compile(r"^/v1/memories/(?P<id>[^/]+)$"),
     lambda b, q, body, m: b.forget_memory(m["id"], q.get("container"), _truthy(q.get("confirm"))), 200),
    ("POST", re.compile(r"^/v1/ask$"), _ask, 200),
    # Media memory (local only for now): its own store, search and ask.
    ("POST", re.compile(r"^/v1/media$"), _add_media, 202),
    ("GET", re.compile(r"^/v1/media$"), lambda b, q, body, m: b.media.list(q.get("container")), 200),
    ("POST", re.compile(r"^/v1/media/search$"), _search_media, 200),
    ("POST", re.compile(r"^/v1/media/ask$"), _ask_media, 200),
    ("GET", re.compile(r"^/v1/media/(?P<id>[^/]+)$"), lambda b, q, body, m: b.media.get(m["id"]), 200),
    ("DELETE", re.compile(r"^/v1/media/(?P<id>[^/]+)$"),
     lambda b, q, body, m: b.media.delete(m["id"], _truthy(q.get("confirm"))), 200),
]


def error_body(status: int, code: str, message: str, details: Any = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details,
                      "retryable": status == 429, "trace_id": ""}}


def dispatch(backend: LocalBackend, method: str, path: str, query: _Query, body: _Body | None) -> tuple[int, Any]:
    """``(status, json)`` for one request, errors in the platform's envelope."""
    for verb, pattern, handler, ok in ROUTES:
        m = pattern.match(path)
        if m and verb == method.upper():
            try:
                params = {k: unquote(v) for k, v in m.groupdict().items()}
                return ok, handler(backend, query, body or {}, params)
            except LocalError as e:
                return e.status, error_body(e.status, e.code, e.message, e.details)
            except RuntimeError as e:
                return 422, error_body(422, "capability_unavailable", str(e))
            except Exception as e:  # noqa: BLE001 - every failure answers in the envelope
                return 500, error_body(500, "internal", f"{type(e).__name__}: {e}")
    return 404, error_body(404, "not_found", f"no route {method.upper()} {path}")
