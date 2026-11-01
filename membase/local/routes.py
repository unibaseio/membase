"""The hosted memory API's ``/v1`` routes, answered by a :class:`LocalBackend`: the same requests,
answers and refusals, so code written for one runs against the other.

- No request names a Memory (a container, here): reads cover every container in the store, writes
  land in its write target (:meth:`LocalBackend.write_target`).
- Ids are typed and opaque (``mem_``, ``doc_``, ``med_``; :mod:`membase.local.ids`), and no answer
  carries a container id: a Memory is named by its name, for attribution only.
- Every list is ``{items, has_more, partial}``: the newest ``limit`` (1–1000, default 100).
- Requests refuse fields they do not know (422).
- A removal without ``confirm=true`` changes nothing and answers 428 ``confirmation_required``.
"""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote, unquote

from . import ids
from .backend import LocalBackend, LocalError

_Body = dict[str, Any]
_Query = dict[str, str]

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000
_SEARCH_LIMIT_MAX = 50


@dataclass
class Raw:
    """A non-JSON answer: a media file or thumbnail."""

    body: bytes
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)


# ---- requests -----------------------------------------------------------------------------------


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes"}


def _invalid(fields: list[tuple[list[str], str, str]]) -> LocalError:
    """The hosted API's answer to a request it cannot parse: 422 ``validation`` naming each field."""
    return LocalError(422, "validation", "Request validation failed.", {
        "fields": [{"loc": loc, "message": msg, "type": typ} for loc, msg, typ in fields],
    })


def _only(body: _Body, allowed: set[str], required: tuple[str, ...] = ()) -> _Body:
    problems = [(["body", k], "Extra inputs are not permitted", "extra_forbidden")
                for k in sorted(set(body) - allowed)]
    problems += [(["body", k], "Field required", "missing") for k in required if k not in body]
    if problems:
        raise _invalid(problems)
    return body


def _int_or_none(value: Any, loc: list[str], low: int, high: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _invalid([(loc, "Input should be a valid integer", "int_type")])
    try:
        n = int(value)
    except ValueError:
        raise _invalid([(loc, "Input should be a valid integer", "int_parsing")]) from None
    if not low <= n <= high:
        raise _invalid([(loc, f"Input should be between {low} and {high}", "range")])
    return n


def _list_limit(q: _Query) -> int:
    """A list's ``limit``: not a number is a 422, out of 1–1000 a 400, as hosted."""
    raw = q.get("limit")
    if raw is None:
        return _DEFAULT_LIMIT
    try:
        n = int(raw)
    except ValueError:
        raise _invalid([(["query", "limit"], "Input should be a valid integer", "int_parsing")]) from None
    if not 1 <= n <= _MAX_LIMIT:
        raise LocalError(400, "validation", f"limit is 1–{_MAX_LIMIT}", {"limit": n})
    return n


def _choice(value: Any, loc: list[str], choices: tuple[str, ...]) -> Any:
    if value is not None and value not in choices:
        raise _invalid([(loc, f"Input should be {' or '.join(map(repr, choices))}", "literal_error")])
    return value


def _text(body: _Body, key: str, *, max_length: int | None = None) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        raise _invalid([(["body", key], "Input should be a valid string", "string_type")])
    if max_length is not None and len(value) > max_length:
        raise _invalid([(["body", key], f"String should have at most {max_length} characters",
                         "string_too_long")])
    return value


# ---- answers ------------------------------------------------------------------------------------


def _list(rows: list, limit: int, partial: list | None = None) -> dict:
    return {"items": rows[:limit], "has_more": len(rows) > limit, "partial": partial or []}


def _partial(containers: list[dict]) -> list[dict]:
    out = []
    for c in containers or []:
        reason = "error" if c.get("error") else ("truncated" if c.get("truncated") else "")
        if reason:
            out.append({"memory_name": c.get("name") or "", "reason": reason})
    return out


def _memory_out(row: dict) -> dict:
    return {
        "id": ids.encode("memory", row["id"]),
        "content": row.get("content") or "",
        **({"title": row["title"]} if row.get("title") else {}),
        "memory_name": row.get("container_name") or "",
        "source_time": row.get("source_time"),
        "recorded_at": row.get("recorded_at"),
    }


def _names(b: LocalBackend) -> dict[str, str]:
    return {cid: b._meta(cid)["name"] for cid in b._ids()}


def _document_out(row: dict, names: dict[str, str]) -> dict:
    cid = str(row.get("container") or "")
    out = {
        "id": ids.encode("document", f"{cid}:{row['id']}"),
        "kind": "note" if (row.get("metadata") or {}).get("kind") == "memory" else "file",
        "title": row.get("title") or "",
        "memory_name": names.get(cid, ""),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    if row.get("custom_id"):
        out["custom_id"] = row["custom_id"]
    # Local learning is done before a write returns: whatever is listed has been learned.
    out["learning"] = [{"memory_name": out["memory_name"], "status": "succeeded"}]
    return out


def _receipt(b: LocalBackend, out: dict, *, kind: str) -> dict:
    """A write's answer: the document it landed as, and where its learning stands — done, here,
    since the local engine learns before it answers."""
    cid = str(out.get("container") or "")
    doc = out.get("document_id")
    name = _names(b).get(cid, "")
    return {
        "id": ids.encode("document", f"{cid}:{doc}") if doc else None,
        "kind": kind,
        "status": "succeeded",
        "learning": [{"memory_name": name, "status": "succeeded"}],
    }


def _search_item(hit: dict) -> dict:
    source = str(hit.get("source") or "")
    kind, _, rest = source.partition(":")
    item: dict = {
        "type": {"episode": "memory", "document": "document"}.get(kind, "passage"),
        "content": hit.get("content") or "",
        "score": hit.get("score"),
        "memory_name": hit.get("container_name") or "",
    }
    if kind == "episode" and rest:
        item["id"] = ids.encode("memory", rest)
    if hit.get("title"):
        item["title"] = hit["title"]
    return item


def _media_out(row: dict) -> dict:
    """One photo, video or recording as the hosted API shows it: its file and thumbnails are
    routes under its public id, not paths on this machine."""
    pid = ids.encode("media", f"{row['container']}:{row['id']}")
    base = f"/v1/media/{pid}"

    def thumb(name: str | None) -> str | None:
        return f"{base}/thumbs/{quote(name, safe='/')}" if name else None

    out = {
        "id": pid,
        "kind": row.get("kind"),
        "filename": row.get("filename"),
        "memory_name": row.get("container_name") or "",
        "summary": row.get("summary") or "",
        "duration": row.get("duration"),
        "created_at": row.get("created_at"),
        "file_url": f"{base}/file" if row.get("path") else None,
    }
    if row.get("custom_id"):
        out["custom_id"] = row["custom_id"]
    cover = row.get("cover")
    out["thumb_url"] = thumb(cover) if cover else (out["file_url"] if row.get("kind") == "image" else None)
    moments = row.get("moments")
    if isinstance(moments, list):
        out["moments"] = [
            {**{k: v for k, v in m.items() if k != "thumb"}, "thumb_url": thumb(m.get("thumb"))}
            for m in moments
        ]
    else:
        out["moments"] = moments
    return out


def _confirm_or(out: dict, public_id: str, done: str) -> dict:
    """The store's ``status: confirmation_required`` is, here as hosted, a 428: nothing changed."""
    if (out or {}).get("status") == "confirmation_required":
        details = {"id": public_id, **{k: out[k] for k in ("verb", "content", "title", "filename", "kind", "how")
                                       if k in out}}
        raise LocalError(428, "confirmation_required", str(out.get("how") or "Pass confirm=true."), details)
    return {"id": public_id, "status": done}


def _named(public_id: str, call: Callable[[], Any]) -> Any:
    """A not-found names the id the caller sent, not the store's own."""
    try:
        return call()
    except LocalError as e:
        if e.status == 404:
            raise LocalError(404, e.code, e.message, {"id": public_id}) from None
        raise


def _in_reach(b: LocalBackend, kind: ids.Kind, public_id: str) -> tuple[str, str]:
    cid, raw = ids.decode(kind, public_id)
    if cid not in b._ids():
        raise LocalError(404, "not_found", f"{kind} not found", {"id": public_id})
    return cid, raw


# ---- routes -------------------------------------------------------------------------------------


def _search(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    body = _only(body, {"q", "limit", "mode"}, required=("q",))
    limit = _int_or_none(body.get("limit"), ["body", "limit"], 1, _SEARCH_LIMIT_MAX)
    mode = _choice(body.get("mode", "full"), ["body", "mode"], ("full", "fast"))
    out = b.search(_text(body, "q"), None, limit, mode=mode)
    return {"items": [_search_item(h) for h in out["results"]], "partial": _partial(out["containers"])}


def _list_memories(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    out = b.facts(_list_limit(q))
    return {"items": [_memory_out(r) for r in out["memories"]], "has_more": out["has_more"],
            "partial": _partial(out["containers"])}


def _get_memory(b: LocalBackend, q: _Query, body: _Body, m) -> Any:
    def read() -> dict:
        cid, raw = _in_reach(b, "memory", m["id"])
        return _memory_out(b.fact(f"{cid}:{raw}"))

    return _named(m["id"], read)


def _add_memory(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    body = _only(body, {"content", "title"}, required=("content",))
    content = _text(body, "content", max_length=20_000)
    if not content:
        raise _invalid([(["body", "content"], "String should have at least 1 character", "string_too_short")])
    title = _text(body, "title", max_length=200)
    out = b.add_memory(content, b.write_target(), title=title)
    return _receipt(b, out, kind="note")


def _forget_memory(b: LocalBackend, q: _Query, body: _Body, m) -> Any:
    def forget() -> dict:
        cid, raw = _in_reach(b, "memory", m["id"])
        return b.forget_memory(f"{cid}:{raw}", confirm=_truthy(q.get("confirm")))

    return _confirm_or(_named(m["id"], forget), m["id"], "forgotten")


def _list_documents(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    kind = _choice(q.get("kind"), ["query", "kind"], ("file", "note"))
    size = _list_limit(q)
    names = _names(b)
    rows = [_document_out(r, names) for r in b.documents()["documents"]]
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    return _list(rows, size)


def _document(b: LocalBackend, public_id: str) -> dict:
    cid, raw = _in_reach(b, "document", public_id)
    row = b.document(raw)
    if row.get("container") != cid:
        raise LocalError(404, "not_found", "document not found", {"id": public_id})
    return row


def _get_document(b: LocalBackend, q: _Query, body: _Body, m) -> Any:
    return _document_out(_named(m["id"], lambda: _document(b, m["id"])), _names(b))


def _add_document(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    body = _only(body, {"content", "url", "title", "custom_id"})
    content = body.get("content")
    url = body.get("url")
    if bool(content) == bool(url):
        raise LocalError(400, "validation", "give exactly one of content or url")
    out = b.add_document(
        b.write_target(),
        content=_text(body, "content") if content else None,
        url=_text(body, "url") if url else None,
        title=_text(body, "title", max_length=200),
        custom_id=_text(body, "custom_id", max_length=200),
    )
    return _receipt(b, out, kind="file")


def _delete_document(b: LocalBackend, q: _Query, body: _Body, m) -> Any:
    def delete() -> dict:
        _document(b, m["id"])
        _, raw = ids.decode("document", m["id"])
        return b.delete_document(raw, confirm=_truthy(q.get("confirm")))

    return _confirm_or(_named(m["id"], delete), m["id"], "deleted")


def _capabilities(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    from ..media.requirements import media_unavailable

    media = media_unavailable() is None
    actions = [a for a in ACTIONS if media or not a.startswith("media.")]
    tools = sorted({ACTIONS[a] for a in actions} | {"get_profile", "memory_rules", "ask_agent"})
    memories = [{"name": b._meta(cid)["name"], "kind": "text"} for cid in b._ids()]
    if media:
        memories += [{"name": b._meta(cid)["name"], "kind": "media"} for cid in b.media._ids()]
    return {"principal": "local", "surfaces": ["rest"], "tools": tools, "features": [],
            "actions": sorted(actions), "memories": memories}


def _ask(b: LocalBackend, q: _Query, body: _Body, _m) -> Any:
    body = _only(body, {"message", "model"})
    return b.ask(body.get("message") or "", model=body.get("model"))


#: Each action of the memory API and the protocol grant it stands on, as hosted; local memory holds
#: every one (the media ones with the media extra installed).
ACTIONS: dict[str, str] = {
    "search": "search_memories",
    "memories.list": "search_memories",
    "memories.get": "search_memories",
    "memories.add": "add_memory",
    "memories.forget": "forget_memory",
    "documents.list": "list_documents",
    "documents.get": "get_document",
    "documents.add": "add_document",
    "documents.delete": "delete_document",
    "media.list": "list_media",
    "media.get": "get_media",
    "media.search": "search_media",
    "media.delete": "delete_media",
}

ROUTES: list[tuple[str, re.Pattern, Callable[..., Any], int]] = [
    ("POST", re.compile(r"^/v1/search$"), _search, 200),
    ("GET", re.compile(r"^/v1/memories$"), _list_memories, 200),
    ("POST", re.compile(r"^/v1/memories/add$"), _add_memory, 202),
    ("GET", re.compile(r"^/v1/memories/(?P<id>[^/]+)$"), _get_memory, 200),
    ("POST", re.compile(r"^/v1/memories/(?P<id>[^/]+)/forget$"), _forget_memory, 200),
    ("GET", re.compile(r"^/v1/documents$"), _list_documents, 200),
    ("POST", re.compile(r"^/v1/documents/add$"), _add_document, 202),
    ("GET", re.compile(r"^/v1/documents/(?P<id>[^/]+)$"), _get_document, 200),
    ("POST", re.compile(r"^/v1/documents/(?P<id>[^/]+)/delete$"), _delete_document, 200),
    ("GET", re.compile(r"^/v1/profile$"), lambda b, q, body, m: b.profile(q.get("q")), 200),
    ("GET", re.compile(r"^/v1/rules$"), lambda b, q, body, m: b.rules(), 200),
    ("POST", re.compile(r"^/v1/ask$"), _ask, 200),
    ("GET", re.compile(r"^/v1/capabilities$"), _capabilities, 200),
]

from ..media.routes import ROUTES as _MEDIA_ROUTES  # noqa: E402

ROUTES += _MEDIA_ROUTES


def error_body(status: int, code: str, message: str, details: Any = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details,
                      "retryable": status == 429, "trace_id": ""}}


def file_answer(path, range_header: str = "") -> tuple[int, Raw]:
    """A file, whole (200) or the part a ``Range: bytes=a-b`` asks for (206)."""
    data = path.read_bytes()
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store"}
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", range_header or "")
    if m and (m[1] or m[2]) and data:
        size = len(data)
        if m[1]:
            start, end = int(m[1]), int(m[2]) if m[2] else size - 1
        else:
            start, end = max(0, size - int(m[2])), size - 1
        if start >= size or start > end:
            raise LocalError(416, "range_not_satisfiable", "range not satisfiable", {"size": size})
        end = min(end, size - 1)
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return 206, Raw(data[start:end + 1], kind, headers)
    return 200, Raw(data, kind, headers)


def dispatch(
    backend: LocalBackend,
    method: str,
    path: str,
    query: _Query,
    body: _Body | None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """``(status, answer)`` for one request: JSON, or :class:`Raw` bytes for a media file; errors
    in the platform's envelope."""
    served = False
    for verb, pattern, handler, ok in ROUTES:
        m = pattern.match(path)
        served = served or bool(m)
        if m and verb == method.upper():
            try:
                if body is not None and not isinstance(body, dict):
                    raise _invalid([(["body"], "Input should be a valid dictionary", "dict_type")])
                params = {k: unquote(v) for k, v in m.groupdict().items()}
                params["headers"] = {k.lower(): v for k, v in (headers or {}).items()}
                out = handler(backend, query, body or {}, params)
                if isinstance(out, tuple):
                    return out
                return ok, out
            except LocalError as e:
                return e.status, error_body(e.status, e.code, e.message, e.details)
            except RuntimeError as e:
                return 422, error_body(422, "capability_unavailable", str(e))
            except Exception as e:  # noqa: BLE001 - every failure answers in the envelope
                return 500, error_body(500, "internal", f"{type(e).__name__}: {e}")
    if served:
        return 405, error_body(405, "method_not_allowed", f"{method.upper()} is not allowed on {path}")
    return 404, error_body(404, "not_found", f"no route {method.upper()} {path}")
