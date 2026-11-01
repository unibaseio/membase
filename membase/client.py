"""The Membase client, hosted or local."""

from __future__ import annotations

import os
import random
import time
from typing import Any
from urllib.parse import quote

import httpx

from ._version import __version__
from .errors import APIConnectionError, APITimeoutError, error_for
from .media.client import Media

DEFAULT_BASE_URL = "https://api.app.membase.com"
# The first search after a quiet spell can take up to a minute while the memory wakes.
DEFAULT_TIMEOUT = 90.0
DEFAULT_MAX_RETRIES = 2
_RETRY_STATUSES = {408, 409, 429}


class Membase:
    """Membase memory: hosted with an API key, or local with ``local=True``."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | None = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx.Client | None = None,
        local: str | os.PathLike | bool | None = None,
    ) -> None:
        """Hosted with an API key; local with ``local=True`` (``~/.membase``) or a directory."""
        if local is None and api_key is None and not os.environ.get("MEMBASE_API_KEY"):
            env = (os.environ.get("MEMBASE_LOCAL") or "").strip()
            local = True if env.lower() in {"1", "true", "yes"} else (env or None)
        if local not in (None, False):
            from .requirements import require_local

            require_local()
            from .local import LocalTransport

            self.api_key = ""
            self.base_url = LocalTransport.BASE_URL
            self.max_retries = 0
            self._owns_http = True
            self._http = httpx.Client(
                base_url=self.base_url,
                transport=LocalTransport(None if local is True else local),
                timeout=None,
            )
            self.documents = _Documents(self)
            self.memories = _Memories(self)
            self.profile_facts = _ProfileFacts(self)
            self.media = Media(self)
            return
        api_key = api_key if api_key is not None else os.environ.get("MEMBASE_API_KEY")
        if not api_key:
            raise ValueError(
                "no API key: pass api_key= or set MEMBASE_API_KEY "
                "(Connect › Developer keys in the Membase app), or use local=True"
            )
        self.api_key = api_key
        self.base_url = (base_url or os.environ.get("MEMBASE_BASE_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self.max_retries = max(0, int(max_retries))
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(base_url=self.base_url, timeout=timeout)
        self.documents = _Documents(self)
        self.memories = _Memories(self)
        self.profile_facts = _ProfileFacts(self)
        self.media = Media(self)

    def add(
        self,
        content: str | None = None,
        *,
        url: str | None = None,
        title: str = "",
        custom_id: str = "",
    ) -> dict:
        """Hand in a document (its text, or a public ``url`` to fetch); the same as
        ``documents.add``."""
        return self.documents.add(content, url=url, title=title, custom_id=custom_id)

    def search(self, q: str, *, limit: int | None = None, mode: str = "full") -> dict:
        """The passages the Memories in reach hold on ``q``, most relevant first: ``items`` (each
        a ``memory`` with its ``mem_`` id, or a ``document`` passage, naming its ``memory_name``)
        and ``partial`` (the Memories that could not answer). ``limit`` is how many in all (1-50);
        ``mode="fast"`` is one round of retrieval with no model call."""
        body: dict[str, Any] = {"q": q, "mode": mode}
        if limit is not None:
            body["limit"] = limit
        return self._request("POST", "/v1/search", json=body)

    def ask(self, message: str) -> dict:
        """One answer to ``message``, written by the memory from the Memories in reach:
        ``{answer, citations: [{memory_name}], partial}``. The passages are not returned. Needs
        a credential that holds ``ask`` (a marketplace buyer's)."""
        return self._request("POST", "/v1/ask", json={"message": message})

    def capabilities(self) -> dict:
        """What this key may do: ``actions`` (``search``, ``memories.add``, …), the Memories it
        reaches (by name and kind), the user's standing ``rules`` (``citation_required``,
        ``retention_notice``, ``version``) and ``features``. Not permission: every call is still
        checked when made."""
        return self._request("GET", "/v1/capabilities")

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Membase:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": f"unibaseio-membase-python/{__version__}",
        }
        attempt = 0
        while True:
            try:
                r = self._http.request(method, path, json=json, params=params, headers=headers)
            except httpx.TimeoutException as e:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(_backoff(attempt))
                    continue
                raise APITimeoutError(f"{method} {path} timed out") from e
            except httpx.HTTPError as e:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(_backoff(attempt))
                    continue
                raise APIConnectionError(f"{method} {path}: {e}") from e
            if 200 <= r.status_code < 300:
                return r.json() if r.content else None
            if (
                r.status_code in _RETRY_STATUSES or r.status_code >= 500
            ) and attempt < self.max_retries:
                attempt += 1
                time.sleep(_backoff(attempt, r.headers.get("Retry-After")))
                continue
            try:
                body = r.json()
            except ValueError:
                body = {"error": {"message": r.text}}
            raise error_for(r.status_code, body)


def _backoff(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    return min(0.5 * (2 ** (attempt - 1)), 8.0) * (0.5 + random.random())


def _limit(limit: int | None, **extra: Any) -> dict | None:
    params = {k: v for k, v in extra.items() if v is not None}
    if limit is not None:
        params["limit"] = limit
    return params or None


def _path(collection: str, item_id: str) -> str:
    return f"/v1/{collection}/{quote(item_id, safe='')}"


class _Memories:
    """The facts the Memories in reach learned (``mem_`` ids)."""

    def __init__(self, client: Membase) -> None:
        self._c = client

    def list(self, *, limit: int | None = None) -> dict:
        """The newest facts (``limit`` 1-1000, default 100): ``{items, has_more, partial}``."""
        return self._c._request("GET", "/v1/memories", params=_limit(limit))

    def get(self, memory_id: str) -> dict:
        """One fact, at any age."""
        return self._c._request("GET", _path("memories", memory_id))

    def add(self, content: str, *, title: str = "") -> dict:
        """Save one fact into this key's write target. The receipt is the note it was written as:
        ``{id: "doc_…", kind: "note", status, learning}``."""
        return self._c._request("POST", "/v1/memories/add", json={"content": content, "title": title})

    def forget(self, memory_id: str, *, confirm: bool = False) -> dict:
        """Forget one learned fact (the material it came from stays). Needs Full access and
        ``confirm=True``; without it nothing changes and :class:`ConfirmationRequiredError` is
        raised."""
        return self._c._request("POST", _path("memories", memory_id) + "/forget", params={"confirm": _flag(confirm)})


class _ProfileFacts:
    """Who the user is: standing statements (name, role, lasting preferences), the account's
    rather than a Memory's (``pf_`` ids). Each needs the key's profile tick."""

    def __init__(self, client: Membase) -> None:
        self._c = client

    def list(self, *, limit: int | None = None) -> dict:
        """The statements, newest first (``limit`` 1-1000, default 100): ``{items: [{id, content,
        source_kind, recorded_at}], has_more, partial}``."""
        return self._c._request("GET", "/v1/profile-facts", params=_limit(limit))

    def add(self, content: str) -> dict:
        """Keep one statement about the user. The answer is the statement and ``created``
        (``False`` when the same one was already kept)."""
        return self._c._request("POST", "/v1/profile-facts/add", json={"content": content})

    def forget(self, fact_id: str, *, confirm: bool = False) -> dict:
        """Forget one statement. Needs Full access and ``confirm=True``; without it nothing
        changes and :class:`ConfirmationRequiredError` is raised."""
        return self._c._request(
            "POST", _path("profile-facts", fact_id) + "/forget", params={"confirm": _flag(confirm)}
        )


class _Documents:
    """The material handed in (``doc_`` ids): files and pages (``kind="file"``) and the notes facts
    were written as (``kind="note"``)."""

    def __init__(self, client: Membase) -> None:
        self._c = client

    def list(self, *, kind: str | None = None, limit: int | None = None) -> dict:
        """The documents, newest first (``limit`` 1-1000, default 100): ``{items, has_more, partial}``."""
        return self._c._request("GET", "/v1/documents", params=_limit(limit, kind=kind))

    def get(self, document_id: str) -> dict:
        """One document, with ``learning``: how far each Memory got in learning it."""
        return self._c._request("GET", _path("documents", document_id))

    def add(
        self,
        content: str | None = None,
        *,
        url: str | None = None,
        title: str = "",
        custom_id: str = "",
    ) -> dict:
        """Hand in a document (its text, or a public ``url`` to fetch) to this key's write target.
        The same ``custom_id`` again is a no-op. The receipt is ``{id: "doc_…", kind: "file",
        status, learning}``."""
        body: dict[str, Any] = {"title": title, "custom_id": custom_id}
        if content is not None:
            body["content"] = content
        if url is not None:
            body["url"] = url
        return self._c._request("POST", "/v1/documents/add", json=body)

    def delete(self, document_id: str, *, confirm: bool = False) -> dict:
        """Withdraw a document and what every Memory learned from it (hosted, the file goes to
        the Files trash). Needs Full access and ``confirm=True``; without it nothing changes and
        :class:`ConfirmationRequiredError` is raised."""
        return self._c._request("POST", _path("documents", document_id) + "/delete", params={"confirm": _flag(confirm)})


def _flag(v: bool) -> str:
    return "true" if v else "false"
