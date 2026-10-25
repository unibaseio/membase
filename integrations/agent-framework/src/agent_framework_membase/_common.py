"""Shared plumbing: which Membase client serves a session, running SDK calls off the loop,
formatting results and turning SDK errors into sentences a person can act on."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from membase import Membase
from membase.errors import (
    APITimeoutError,
    MembaseError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

logger = logging.getLogger("agent_framework_membase")

# The package's own default: it does not depend on which SDK release is installed.
DEFAULT_BASE_URL = "https://api.app.membase.com"

DEFAULT_SOURCE_ID = "membase"
"""The provider's ``source_id``; also the key under ``session.state`` holding the user binding."""

USER_ID_KEY = "user_id"

T = TypeVar("T")

ClientFor = Callable[[str], Membase]

REVOKED = "this Membase key was narrowed or revoked by its owner, or it does not allow this operation"


def bind_user(session: Any, user_id: str, *, source_id: str = DEFAULT_SOURCE_ID) -> None:
    """Record which person a session belongs to, for ``client_for``.

    Stored as ``session.state[source_id]["user_id"]``: the provider-scoped state slot the
    framework hands to the provider, so it is persisted with the session.
    """
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id must be a non-empty string")
    session.state.setdefault(source_id, {})[USER_ID_KEY] = user_id.strip()


def user_of(session: Any, source_id: str = DEFAULT_SOURCE_ID) -> str | None:
    """The user id bound to ``session`` with :func:`bind_user`, if any."""
    state = getattr(session, "state", None)
    if not isinstance(state, Mapping):
        return None
    slot = state.get(source_id)
    if not isinstance(slot, Mapping):
        return None
    value = slot.get(USER_ID_KEY)
    return value.strip() if isinstance(value, str) and value.strip() else None


class NoUserBound(Exception):
    """``client_for`` is configured but the session names no user."""


class Clients:
    """One client for every call, or one client per person via ``client_for``."""

    def __init__(
        self,
        client: Membase | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        local: str | os.PathLike[str] | bool | None = None,
        client_for: ClientFor | None = None,
        source_id: str = DEFAULT_SOURCE_ID,
    ) -> None:
        given = client is not None or api_key is not None or base_url is not None or local is not None
        if client is not None and (api_key is not None or base_url is not None or local is not None):
            raise ValueError("pass either client= or api_key/base_url/local, not both")
        if client_for is not None and given:
            raise ValueError(
                "client_for serves each person with their own client; do not also pass a "
                "shared client, api_key, base_url or local"
            )
        self.source_id = source_id
        self.client_for = client_for
        self._owned = False
        self.client: Membase | None = None
        if client_for is None:
            if client is None:
                # MEMBASE_API_KEY / MEMBASE_BASE_URL / MEMBASE_LOCAL apply when nothing is passed.
                base_url = base_url or os.environ.get("MEMBASE_BASE_URL") or DEFAULT_BASE_URL
                client = Membase(api_key=api_key, base_url=base_url, local=local)
                self._owned = True
            self.client = client

    def for_session(self, session: Any) -> Membase:
        """The client that may act for this session's user. Never another person's."""
        if self.client_for is None:
            assert self.client is not None
            return self.client
        user_id = user_of(session, self.source_id)
        if user_id is None:
            raise NoUserBound(
                "no Membase user is bound to this session; call "
                f"bind_user(session, user_id) (session.state[{self.source_id!r}]['user_id'])"
            )
        return self.client_for(user_id)

    def close(self) -> None:
        if self._owned and self.client is not None:
            self.client.close()


async def in_thread(fn: Callable[..., T], /, *args: Any, timeout: float | None = None, **kwargs: Any) -> T:
    """Run a blocking SDK call in a worker thread; with ``timeout``, stop waiting after it.

    A call that outlives the budget is not cancelled (a thread cannot be): it finishes in the
    background, which also wakes a cold memory for the next turn, and a late failure is logged.
    """
    task = asyncio.ensure_future(asyncio.to_thread(functools.partial(fn, *args, **kwargs)))
    if timeout is None:
        return await task
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        task.add_done_callback(_log_late_failure)
        raise


def _log_late_failure(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Membase: a call that ran past its budget later failed: %s", describe_error(exc))


def describe_error(exc: BaseException) -> str:
    """One actionable sentence for an SDK failure."""
    if isinstance(exc, PermissionDeniedError):
        return f"Membase refused the call (403): {REVOKED}. Do not retry with this key. ({exc.message})"
    if isinstance(exc, APITimeoutError):
        return (
            "Membase did not answer in time; the memory may be waking up after a quiet spell. "
            "Try again in a minute."
        )
    if isinstance(exc, RateLimitError):
        return f"Membase is busy for this account right now (429): {exc.message}"
    if isinstance(exc, UnprocessableEntityError):
        code = f"422 {exc.code}" if exc.code else "422"
        return f"The account's memory cannot run right now ({code}): {exc.message}"
    if isinstance(exc, MembaseError):
        return f"Membase error: {exc}"
    return f"Membase call failed: {exc!r}"


def format_search(answer: Mapping[str, Any] | None) -> tuple[list[str], list[str]]:
    """``(passages, unanswered)``: one line per passage citing its container, one per Memory
    that could not answer (so an empty result is never mistaken for an empty memory)."""
    answer = answer or {}
    passages: list[str] = []
    for hit in answer.get("results") or []:
        if not isinstance(hit, Mapping):
            continue
        content = str(hit.get("content") or "").strip()
        if not content:
            continue
        where = str(hit.get("container_name") or hit.get("container") or "Membase").strip()
        passages.append(f"- [{where}] {content}")
    unanswered: list[str] = []
    for c in answer.get("containers") or []:
        if isinstance(c, Mapping) and c.get("error"):
            name = c.get("name") or c.get("id") or "a Memory"
            unanswered.append(f'- "{name}" could not answer: {c["error"]}')
    return passages, unanswered
