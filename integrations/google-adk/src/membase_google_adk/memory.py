"""Membase memory for Google ADK: a ``BaseMemoryService`` and three deliberate tools.

* :class:`MembaseMemoryService` is what ADK's ``preload_memory`` and ``load_memory``
  tools search through ``tool_context.search_memory``. Its search is time-boxed (8 s by
  default) because the first Membase search after a quiet spell can take up to a minute
  while the memory wakes; a recall that runs out of budget raises, ``preload_memory``
  logs it and the turn goes on without memory (fail open). Writing whole sessions is
  off by default (``capture=False``): Membase saves what the user asked to keep.
* :func:`membase_tools` gives the model ``membase_search``, ``membase_save`` and
  ``membase_profile``: deliberate calls with the SDK's full timeout, whose answers cite
  the Memory each passage came from and turn errors into a sentence.

One Membase key (or one local directory) is one person's memory. ADK passes the user
id to both, so a multi-user app gives ``client_for`` (user id -> that user's client).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from google.adk.memory import BaseMemoryService
from google.adk.memory.base_memory_service import SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.tools import FunctionTool, ToolContext
from google.genai import types
from membase import Membase
from membase.errors import (
    APIConnectionError,
    BadRequestError,
    MembaseError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
    WriteTargetRequiredError,
)

if TYPE_CHECKING:
    from google.adk.events.event import Event
    from google.adk.sessions.session import Session

logger = logging.getLogger(__name__)

T = TypeVar("T")

ClientFor = Callable[[str], Membase]
"""Returns the Membase client of one end user: their own key, or their own local directory."""

# The package's own default: it does not depend on which SDK release is installed.
DEFAULT_BASE_URL = "https://api.app.membase.com"
DEFAULT_RECALL_TIMEOUT = 8.0
DEFAULT_MAX_SEARCH_RESULTS = 5
ALL_TOOLS = ("search", "save", "profile")

REVOKED_MESSAGE = (
    "Membase refused this request (403): this Membase key was narrowed or revoked by its owner, "
    "or it does not allow this operation. Ask the owner for access; do not retry with this key."
)
WRITE_TARGET_MESSAGE = (
    "This Membase key reaches several Memories and has no write target, so nothing was saved. "
    "Give the key a single Memory under Connect › Developer keys in the Membase app."
)
TIMEOUT_MESSAGE = (
    "Membase did not answer in time. The memory may still be waking up after a quiet spell; try again in a minute."
)


class MembaseRecallTimeout(TimeoutError):
    """A search ran past ``recall_timeout``; ``preload_memory`` logs it and continues without memory."""


def _resolve(client: Membase | None, client_for: ClientFor | None, user_id: str | None) -> Membase:
    if client_for is not None:
        if not user_id:
            raise ValueError("client_for is set, so every call needs the end user's id")
        return client_for(user_id)
    if client is None:  # pragma: no cover - the constructors always set one
        raise ValueError("no Membase client")
    return client


def _build_client(
    client: Membase | None, api_key: str | None, base_url: str | None, local: str | bool | None
) -> Membase:
    if client is not None:
        return client
    base_url = base_url or os.environ.get("MEMBASE_BASE_URL") or DEFAULT_BASE_URL
    return Membase(api_key=api_key, base_url=base_url, local=local)


class MembaseMemoryService(BaseMemoryService):
    """A person's Membase memory as ADK's memory service.

    Args:
        client: A ready :class:`membase.Membase` client, used for every user: right for a
            personal agent. Otherwise one is built from ``api_key`` / ``base_url`` /
            ``local`` (or ``MEMBASE_API_KEY``).
        client_for: ``user_id -> Membase`` for a multi-user app, so each user's search and
            writes go to their own memory. Takes precedence over ``client``.
        api_key: A Membase developer key (Read, or Read & write to save).
        base_url: Overrides ``https://api.app.membase.com`` (or ``MEMBASE_BASE_URL``).
        local: A directory for local mode (``pip install 'unibaseio-membase[local]'``).
        max_search_results: Passages per search. Defaults to 5.
        recall_timeout: Budget in seconds for :meth:`search_memory`. ``None`` waits for
            the SDK's own timeout. Defaults to 8 s.
        capture: Let ADK write sessions and events to Membase. Defaults to ``False``:
            the model saves deliberately with ``membase_save`` instead.
        capture_roles: Whose words a captured session keeps. Defaults to the user's only,
            so assistant answers are not learned as facts.
    """

    def __init__(
        self,
        *,
        client: Membase | None = None,
        client_for: ClientFor | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        local: str | bool | None = None,
        max_search_results: int = DEFAULT_MAX_SEARCH_RESULTS,
        recall_timeout: float | None = DEFAULT_RECALL_TIMEOUT,
        capture: bool = False,
        capture_roles: Sequence[str] = ("user",),
    ) -> None:
        if recall_timeout is not None and recall_timeout <= 0:
            raise ValueError("MembaseMemoryService: recall_timeout must be positive or None")
        self.client_for = client_for
        self.client = None if client_for is not None else _build_client(client, api_key, base_url, local)
        self.max_search_results = max_search_results
        self.recall_timeout = recall_timeout
        self.capture = capture
        self.capture_roles = tuple(capture_roles)

    def _client(self, user_id: str | None) -> Membase:
        return _resolve(self.client, self.client_for, user_id)

    async def search_memory(self, *, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
        """Passages on ``query``, each prefixed with the Memory it came from in brackets.

        Raises :class:`MembaseRecallTimeout` when ``recall_timeout`` runs out; the request
        keeps running in a daemon thread, so the memory is awake for the next turn.
        """
        del app_name
        if not query or not query.strip():
            return SearchMemoryResponse()
        client = self._client(user_id)
        call = lambda: client.search(query, limit=self.max_search_results)  # noqa: E731
        if self.recall_timeout is None:
            payload = await asyncio.to_thread(call)
        else:
            payload = await _within(self.recall_timeout, call)
        return SearchMemoryResponse(memories=_to_entries(payload))

    async def add_session_to_memory(self, session: Session) -> None:
        """Save the session's turns (``capture_roles``) as one note. Only with ``capture=True``.

        It saves the whole session each time, so call it once, when the session ends; use
        :meth:`add_events_to_memory` to save turns as they happen.
        """
        await self.add_events_to_memory(
            app_name=session.app_name, user_id=session.user_id, events=session.events, session_id=session.id
        )

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[Event],
        session_id: str | None = None,
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Save these events' text (``capture_roles``) as one note. Only with ``capture=True``."""
        del app_name, session_id, custom_metadata
        if not self.capture:
            logger.debug("capture is off; not writing the session to Membase")
            return
        lines = []
        for event in events:
            role = "user" if event.author == "user" else "model"
            if role not in self.capture_roles or event.content is None:
                continue
            text = "\n".join(p.text for p in event.content.parts or [] if p.text).strip()
            if text:
                lines.append(f"{role}: {text}" if len(self.capture_roles) > 1 else text)
        if lines:
            client = self._client(user_id)
            await asyncio.to_thread(client.memories.add, "\n\n".join(lines), title="Conversation with an ADK agent")

    async def add_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memories: Sequence[MemoryEntry],
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Save each entry's text as a note in the key's write target (an explicit write)."""
        del app_name, custom_metadata
        client = self._client(user_id)
        for entry in memories:
            text = "\n".join(p.text for p in entry.content.parts or [] if p.text).strip()
            if text:
                await asyncio.to_thread(client.memories.add, text)


def membase_tools(
    client: Membase | None = None,
    *,
    client_for: ClientFor | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    local: str | bool | None = None,
    tools: Iterable[str] = ALL_TOOLS,
) -> list[FunctionTool]:
    """``membase_search``, ``membase_save`` and ``membase_profile`` as ADK tools.

    They share one client, or ``client_for`` with the user id ADK passes in the tool
    context. Drop ``"save"`` for a Read key, ``"profile"`` when the owner did not share it.
    """
    names = tuple(tools)
    unknown = sorted(set(names) - set(ALL_TOOLS))
    if unknown:
        raise ValueError(f"membase_tools: unknown tools {unknown}; choose from {ALL_TOOLS}")
    shared = None if client_for is not None else _build_client(client, api_key, base_url, local)

    def pick(tool_context: ToolContext) -> Membase:
        return _resolve(shared, client_for, getattr(tool_context, "user_id", None))

    async def membase_search(query: str, tool_context: ToolContext, limit: int | None = None) -> str:
        """Search the user's Membase memory for what they told their assistants before and what their Memories
        have read. Each passage names the Memory it came from in [brackets]; cite it. Use it when the answer may
        depend on the user's past, preferences or documents. The first search after a quiet spell can take up to
        a minute.

        Args:
            query: What to look for, in plain words.
            limit: Optional maximum number of passages (1-50).
        """
        return await _tool_call(lambda: _format_search(pick(tool_context).search(query, limit=limit), query))

    async def membase_save(content: str, tool_context: ToolContext) -> str:
        """Save one fact or note to the user's Membase memory. Only when the user asked you to remember it or
        plainly meant it to be kept; save their words, not your summary of the conversation. Never save secrets,
        credentials or what other people said.

        Args:
            content: What to save, in the user's own words.
        """
        if not content or not content.strip():
            return "Nothing to save: content is empty."
        return await _tool_call(lambda: _format_saved(pick(tool_context).memories.add(content)))

    async def membase_profile(tool_context: ToolContext) -> str:
        """Read the user's Membase profile: standing statements about who they are (name, role, lasting
        preferences), newest first. Call it once near the start of a conversation."""
        return await _tool_call(lambda: _format_profile(pick(tool_context).profile_facts.list()))

    chosen = {"search": membase_search, "save": membase_save, "profile": membase_profile}
    return [FunctionTool(chosen[name]) for name in ALL_TOOLS if name in names]


# ---------------------------------------------------------------------- helpers


async def _within(seconds: float, call: Callable[[], T]) -> T:
    """Run a blocking call with a budget. On timeout it keeps running in a daemon thread
    (not the loop's executor, which ``asyncio.run`` waits for on exit) and is abandoned."""
    future: concurrent.futures.Future[T] = concurrent.futures.Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(call())
        except BaseException as error:  # noqa: BLE001 - handed to the awaiting coroutine
            future.set_exception(error)

    threading.Thread(target=run, name="membase-recall", daemon=True).start()
    try:
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), seconds)
    except asyncio.TimeoutError:
        future.add_done_callback(lambda f: f.exception())  # keep a late error retrieved
        logger.warning("Membase recall took longer than %ss (memory may be waking up)", seconds)
        raise MembaseRecallTimeout(f"Membase recall took longer than {seconds}s") from None


def _unavailable(payload: Mapping[str, Any]) -> list[str]:
    """Memories that could not answer (``partial`` with ``reason: error``). A Memory whose
    passages only lost to ``limit`` (``truncated``) did answer."""
    return [str(p.get("memory_name") or "a Memory") for p in payload.get("partial") or [] if p.get("reason") == "error"]


def _to_entries(payload: Mapping[str, Any]) -> list[MemoryEntry]:
    entries = []
    for hit in payload.get("items") or []:
        content = str(hit.get("content") or "").strip()
        if not content:
            continue
        name = hit.get("memory_name") or "Membase"
        metadata = {k: v for k, v in hit.items() if k not in ("content", "id") and v is not None}
        entries.append(
            MemoryEntry(
                content=types.Content(parts=[types.Part(text=f"[{name}] {content}")]),
                id=hit.get("id"),
                custom_metadata=metadata,
            )
        )
    for name in _unavailable(payload):
        entries.append(
            MemoryEntry(
                content=types.Content(parts=[types.Part(text=f"[{name}] This Membase memory could not answer.")]),
                custom_metadata={"memory_name": name, "reason": "error"},
            )
        )
    return entries


def _format_search(payload: Mapping[str, Any], query: str) -> str:
    hits = [h for h in payload.get("items") or [] if str(h.get("content") or "").strip()]
    lines = [f"{i}. [{h.get('memory_name') or 'Membase'}] {str(h['content']).strip()}" for i, h in enumerate(hits, 1)]
    errors = [f"- Memory '{name}' could not answer." for name in _unavailable(payload)]
    if not lines and not errors:
        return f"No Membase memory matched {query!r}."
    out = []
    if lines:
        out.append("Membase passages (cite the Memory in brackets):\n" + "\n".join(lines))
    if errors:
        out.append("Not searched, so missing results are not evidence of absence:\n" + "\n".join(errors))
    return "\n\n".join(out)


def _format_saved(result: Mapping[str, Any] | None) -> str:
    result = result or {}
    names = sorted({str(x.get("memory_name")) for x in result.get("learning") or [] if x.get("memory_name")})
    where = f" in {', '.join(names)}" if names else ""
    if result.get("status") == "succeeded":
        return f"Saved to Membase{where} and learned."
    return f"Saved to Membase{where}. It shows up in search once the Memory has learned it."


def _format_profile(result: Mapping[str, Any]) -> str:
    facts = [str(f.get("content") or "").strip() for f in result.get("items") or []]
    facts = [f for f in facts if f]
    if not facts:
        return "The Membase profile is empty."
    return "About the user:\n" + "\n".join(f"- {f}" for f in facts)


async def _tool_call(call: Callable[[], str]) -> str:
    """Run a blocking SDK call for a tool and turn Membase errors into a sentence."""
    try:
        return await asyncio.to_thread(call)
    except PermissionDeniedError:
        return REVOKED_MESSAGE
    except APIConnectionError:  # includes APITimeoutError
        return TIMEOUT_MESSAGE
    except BadRequestError as error:
        return f"Membase rejected the request: {error.message}"
    except WriteTargetRequiredError:
        return WRITE_TARGET_MESSAGE
    except UnprocessableEntityError as error:
        return (
            "The user's Membase memory cannot run right now (no working model; "
            f"the owner can fix it under AI Setup): {error.message}"
        )
    except RateLimitError:
        return "Membase is busy for this account right now (rate limited); try again shortly."
    except MembaseError as error:
        return f"Membase error: {error}"
