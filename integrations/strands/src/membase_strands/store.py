"""A Strands ``MemoryStore`` backed by Membase.

:class:`MembaseMemoryStore` plugs a person's Membase memory into
:class:`strands.memory.MemoryManager`:

* :meth:`MembaseMemoryStore.search` is what the manager calls before each user turn
  (context injection) and from its built-in ``search_memory`` tool. It is time-boxed
  (8 s by default) because a Membase search is a turn inside the account's own memory
  container, and the first one after a quiet spell can take up to a minute while it
  wakes. A recall that runs out of budget raises, and the manager logs it and carries
  on without memory (fail open); the request keeps running in a daemon thread, so the
  memory is warm for the next turn.
* :meth:`MembaseMemoryStore.add` / :meth:`MembaseMemoryStore.add_messages` are the
  manager's write sinks. They only exist for the manager when ``capture=True``
  (``writable`` follows ``capture``); the default is off, because Membase saves what
  the user asked to keep, in their words, rather than every turn.
* :meth:`MembaseMemoryStore.get_tools` gives the model ``membase_search``,
  ``membase_save`` and ``membase_profile``: deliberate calls with the SDK's full
  timeout, whose results cite the Membase memory (container) each passage came from.

Failure contract (mirrors the SDK's vended stores): ``search``, ``add`` and
``add_messages`` raise on failure, because the manager owns isolation. It logs a failed
search and continues, surfaces a failed ``add`` as ``AggregateMemoryError`` and retries a
failed ``add_messages`` batch. Only the model-facing tools turn errors into text.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from collections.abc import Callable, Iterable, Sequence
from typing import Any, TypeVar

from membase import Membase
from membase.errors import (
    APIConnectionError,
    BadRequestError,
    MembaseError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from strands import tool
from strands.memory import AddMessagesContext, MemoryEntry, MemoryStore, SearchOptions
from strands.types.content import Message
from strands.types.tools import AgentTool

logger = logging.getLogger(__name__)

T = TypeVar("T")

# The package's own default: it does not depend on which SDK release is installed.
DEFAULT_BASE_URL = "https://api.app.membase.com"
DEFAULT_RECALL_TIMEOUT = 8.0
DEFAULT_MAX_SEARCH_RESULTS = 5
DEFAULT_DESCRIPTION = (
    "The user's Membase memory: what they told their assistants and the documents their "
    "Memories have read. Each passage names the Memory it came from."
)
ALL_TOOLS = ("search", "save", "profile")

REVOKED_MESSAGE = (
    "Membase refused this request (403): this Membase key was narrowed or revoked by its owner, "
    "or it does not allow this operation. Ask the owner for access; do not retry with this key."
)
TIMEOUT_MESSAGE = (
    "Membase did not answer in time. The memory may still be waking up after a quiet spell; try again in a minute."
)


class MembaseAccessError(PermissionError):
    """The key was refused (403): its owner narrowed or revoked it. Never retried."""


class MembaseRecallTimeout(TimeoutError):
    """An automatic recall ran past its budget; the manager logs it and continues without memory."""


class MembaseMemoryStore(MemoryStore):
    """One person's Membase memory as a Strands memory store.

    Membase has no ``user_id``: one API key (or one local directory) is one person's
    account, and the ``MemoryStore`` protocol passes no user or session to ``search``.
    So one store serves one person. For a multi-user app, build the store (and the
    agent's ``MemoryManager``) per user, from that user's own key or directory:

    .. code-block:: python

        def agent_for(user):
            store = MembaseMemoryStore(api_key=keys.get(user.id))
            return Agent(memory_manager=MemoryManager(stores=[store], search_tool_config=False))

    Args:
        client: A ready :class:`membase.Membase` client. Otherwise one is built from
            ``api_key`` / ``base_url`` / ``local`` (or ``MEMBASE_API_KEY``).
        api_key: A Membase developer key (Read, or Read & write to save).
        base_url: Overrides ``https://api.app.membase.com`` (or ``MEMBASE_BASE_URL``).
        local: A directory for local mode (``pip install 'unibaseio-membase[local]'``).
        container: The Memory (container id or name) to search and to write captured
            turns to. Omit to search every Memory the key reaches; writes then need it
            when the key reaches more than one.
        name: Store name the manager and its tools use. Defaults to ``"membase"``.
        description: Shown to the model in the manager's search tool description.
        max_search_results: Default passages per search when the caller passes none.
        recall_timeout: Budget in seconds for :meth:`search` (the manager's automatic
            injection and its ``search_memory`` tool). ``None`` waits for the SDK's own
            timeout. Defaults to 8 s.
        capture: Let the manager write to Membase: makes the store ``writable`` so the
            manager's ``add_memory`` tool and automatic ``extraction`` can use
            :meth:`add` / :meth:`add_messages`. Defaults to ``False``.
        extraction: The manager's automatic-extraction config (``True`` or an
            ``ExtractionConfig``); requires ``capture=True``. Without a custom extractor,
            the manager hands raw turns to :meth:`add_messages`, which saves them as one
            note for Membase to learn from.
        capture_roles: Which messages :meth:`add_messages` saves. Defaults to the user's
            own words only, so assistant answers are not learned as facts.
        tools: Which model-facing tools :meth:`get_tools` returns, from
            ``("search", "save", "profile")``. Defaults to all three; drop ``"save"`` for
            a Read key, ``"profile"`` when the owner did not share the profile.
    """

    def __init__(
        self,
        *,
        client: Membase | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        local: str | bool | None = None,
        container: str | None = None,
        name: str = "membase",
        description: str | None = DEFAULT_DESCRIPTION,
        max_search_results: int | None = None,
        recall_timeout: float | None = DEFAULT_RECALL_TIMEOUT,
        capture: bool = False,
        extraction: Any = None,
        capture_roles: Sequence[str] = ("user",),
        tools: Iterable[str] = ALL_TOOLS,
    ) -> None:
        """Build the store. No network call happens here."""
        if extraction and not capture:
            raise ValueError("MembaseMemoryStore: extraction writes to Membase, so it needs capture=True")
        if recall_timeout is not None and recall_timeout <= 0:
            raise ValueError("MembaseMemoryStore: recall_timeout must be positive or None")
        tool_names = tuple(tools)
        unknown = sorted(set(tool_names) - set(ALL_TOOLS))
        if unknown:
            raise ValueError(f"MembaseMemoryStore: unknown tools {unknown}; choose from {ALL_TOOLS}")

        if client is None:
            base_url = base_url or os.environ.get("MEMBASE_BASE_URL") or DEFAULT_BASE_URL
            client = Membase(api_key=api_key, base_url=base_url, local=local)
            self._owns_client = True
        else:
            self._owns_client = False
        self.client = client

        # MemoryStore protocol attributes.
        self.name = name
        self.description = description
        self.max_search_results = max_search_results
        self.writable = capture
        self.extraction = extraction

        self.container = container
        self.recall_timeout = recall_timeout
        self.capture = capture
        self.capture_roles = tuple(capture_roles)
        self.tool_names = tool_names

        self._container_ids: dict[str, str] = {}
        self._recall_inflight: concurrent.futures.Future[Any] | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ search

    async def search(self, query: str, options: SearchOptions | None = None) -> list[MemoryEntry]:
        """Passages on ``query``, most relevant first, each citing its Membase memory.

        Time-boxed by ``recall_timeout`` because the manager calls it on its own before a
        user turn. Raises :class:`MembaseRecallTimeout` when the budget runs out (the
        manager logs it and continues without memory) and :class:`MembaseAccessError` on
        a 403. A Memory that could not answer comes back as an entry saying so, not as
        silence.
        """
        if not query or not query.strip():
            return []
        limit = options.get("max_search_results") if options else None
        if limit is None:
            limit = self.max_search_results or DEFAULT_MAX_SEARCH_RESULTS

        def call() -> list[MemoryEntry]:
            return _to_entries(self._search_raw(query, limit))

        if self.recall_timeout is None:
            return await asyncio.to_thread(_translate_403, call)
        return await self._time_boxed(call)

    async def _time_boxed(self, call: Callable[[], list[MemoryEntry]]) -> list[MemoryEntry]:
        with self._lock:
            pending = self._recall_inflight
            if pending is not None and not pending.done():
                logger.info("store=<%s> | previous Membase recall still waking the memory; skipping", self.name)
                raise MembaseRecallTimeout(
                    "Membase recall skipped: the previous one is still waiting for the memory to wake"
                )
            future: concurrent.futures.Future[list[MemoryEntry]] = concurrent.futures.Future()
            self._recall_inflight = future

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(_translate_403(call))
            except BaseException as error:  # noqa: BLE001 - handed to the awaiting coroutine
                future.set_exception(error)

        # A daemon thread, not the loop's executor: Strands runs a sync ``agent(...)`` call
        # under ``asyncio.run``, which waits for executor threads on exit, so a request left
        # behind there would hold the turn hostage. A daemon thread is simply abandoned.
        threading.Thread(target=run, name="membase-recall", daemon=True).start()
        try:
            return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), self.recall_timeout)
        except asyncio.TimeoutError:
            future.add_done_callback(_drain)
            logger.warning(
                "store=<%s>, budget=<%ss> | Membase recall ran out of time (memory may be waking up); "
                "continuing without memory",
                self.name,
                self.recall_timeout,
            )
            raise MembaseRecallTimeout(
                f"Membase recall took longer than {self.recall_timeout}s; continuing without memory"
            ) from None

    def _search_raw(self, query: str, limit: int | None, container: str | None = None) -> dict[str, Any]:
        target = container if container is not None else self.container
        container_id = self._resolve_container(target) if target else None
        return self.client.search(query, container=container_id, limit=limit)

    # ------------------------------------------------------------------ writes

    async def add(self, content: str, metadata: dict[str, Any] | None = None) -> Any:
        """Save one note to Membase. Only reachable through the manager with ``capture=True``.

        ``metadata`` may carry ``title`` and ``static`` (a standing fact for the profile);
        other keys are not sent, since Membase does not filter on metadata.
        """
        self._require_capture("add")
        if not content or not content.strip():
            raise ValueError("MembaseMemoryStore: content must not be empty")
        meta = metadata or {}
        static = bool(meta.get("static", False))
        title = str(meta.get("title") or "")
        return await asyncio.to_thread(_translate_403, lambda: self._save(content, None, static, title))

    async def add_messages(self, messages: list[Message], context: AddMessagesContext | None = None) -> Any:
        """Save a batch of turns as one note for Membase to learn from (``capture=True`` only).

        Keeps the text of the roles in ``capture_roles`` (the user's own words by default)
        and skips the batch when nothing is left. Errors propagate so the manager retries
        the batch; a retry can save the same note twice.
        """
        del context
        self._require_capture("add_messages")
        lines: list[str] = []
        for message in messages:
            if message.get("role") not in self.capture_roles:
                continue
            text = "\n".join(
                block["text"] for block in message.get("content", []) if isinstance(block, dict) and block.get("text")
            ).strip()
            if text:
                lines.append(f"{message['role']}: {text}" if len(self.capture_roles) > 1 else text)
        if not lines:
            return None
        note = "\n\n".join(lines)
        return await asyncio.to_thread(
            _translate_403, lambda: self._save(note, None, False, "Conversation with a Strands agent")
        )

    def _require_capture(self, method: str) -> None:
        if not self.capture:
            raise ValueError(
                f"MembaseMemoryStore.{method}: capture is off. Writes are deliberate by default: "
                "let the model call membase_save, or build the store with capture=True."
            )

    def _save(self, content: str, container: str | None, static: bool, title: str) -> dict[str, Any]:
        target = container if container is not None else self.container
        container_id = None if static or not target else self._resolve_container(target)
        return self.client.memories.add(content, container=container_id, static=static, title=title)

    # ------------------------------------------------------------------ containers

    def _resolve_container(self, value: str) -> str:
        """A container id for ``value`` (an id or a Memory's name), from ``containers.list``."""
        cached = self._container_ids.get(value)
        if cached:
            return cached
        containers = self.client.containers.list().get("containers", [])
        for c in containers:
            if c.get("id") == value:
                self._container_ids[value] = value
                return value
        wanted = value.strip().casefold()
        for c in containers:
            if str(c.get("name", "")).strip().casefold() == wanted:
                self._container_ids[value] = c["id"]
                return c["id"]
        names = ", ".join(repr(c.get("name")) for c in containers) or "none"
        raise ValueError(f"No Membase memory named {value!r} is in this key's reach (available: {names})")

    # ------------------------------------------------------------------ tools

    def get_tools(self) -> list[AgentTool]:
        """``membase_search`` / ``membase_save`` / ``membase_profile`` for deliberate calls.

        These use the SDK's full timeout (a cold start can take up to a minute) and turn
        errors into text the model can relay.
        """
        store = self
        tools: list[AgentTool] = []

        if "search" in self.tool_names:

            @tool(
                name="membase_search",
                description=(
                    "Search the user's Membase memory for what they told their assistants before and "
                    "what their Memories have read. Each passage names the Memory it came from; cite it. "
                    "Use when the answer may depend on the user's past, preferences or documents."
                ),
            )
            async def membase_search(query: str, container: str | None = None, limit: int | None = None) -> str:
                """Search Membase.

                Args:
                    query: What to look for, in plain words.
                    container: Optional name (or id) of one Memory to search; omit to search all.
                    limit: Optional maximum number of passages (1-50).
                """
                return await _tool_call(lambda: _format_search(store._search_raw(query, limit, container), query))

            tools.append(membase_search)

        if "save" in self.tool_names:

            @tool(
                name="membase_save",
                description=(
                    "Save something to the user's Membase memory. Only when the user asked you to remember "
                    "it or plainly meant it to be kept; save their words, not your summary of the chat. "
                    "Set standing_fact=true for a lasting fact about the user (goes to their profile). "
                    "If the user has several Memories, name the one to save to."
                ),
            )
            async def membase_save(content: str, container: str | None = None, standing_fact: bool = False) -> str:
                """Save to Membase.

                Args:
                    content: What to save, in the user's words.
                    container: Name (or id) of the Memory to save to; needed when there are several.
                    standing_fact: True for a lasting fact about the user, kept in their profile.
                """
                if not content or not content.strip():
                    return "Nothing to save: content is empty."

                def save() -> str:
                    try:
                        return _format_saved(store._save(content, container, standing_fact, ""))
                    except BadRequestError as error:
                        if standing_fact or container or store.container:
                            raise
                        names = [str(c.get("name")) for c in store.client.containers.list().get("containers", [])]
                        return (
                            f"Membase needs to know which Memory to save to ({error.message}). "
                            f"Ask the user, then call membase_save again with container set to one of: {names}"
                        )

                return await _tool_call(save)

            tools.append(membase_save)

        if "profile" in self.tool_names:

            @tool(
                name="membase_profile",
                description=(
                    "Read the user's Membase profile: standing facts about who they are and recent changes. "
                    "Call once near the start of a conversation, optionally with a topic."
                ),
            )
            async def membase_profile(topic: str | None = None) -> str:
                """Read the Membase profile.

                Args:
                    topic: Optional topic to pull relevant profile facts for.
                """
                return await _tool_call(lambda: _format_profile(store.client.profile(q=topic or None)))

            tools.append(membase_profile)

        return tools

    def close(self) -> None:
        """Close the client if this store built it."""
        if self._owns_client:
            self.client.close()


# ---------------------------------------------------------------------- helpers


def _drain(future: concurrent.futures.Future[Any]) -> None:
    """Log the outcome of a recall nobody waited for (and keep its exception retrieved)."""
    if future.cancelled():
        return
    error = future.exception()
    if error is not None:
        logger.info("reason=<%s> | late Membase recall failed", error)
    else:
        logger.info("late Membase recall finished; the memory is awake for the next turn")


def _translate_403(call: Callable[[], T]) -> T:
    try:
        return call()
    except PermissionDeniedError as error:
        raise MembaseAccessError(f"{REVOKED_MESSAGE} ({error.message})") from error


def _to_entries(payload: dict[str, Any]) -> list[MemoryEntry]:
    entries: list[MemoryEntry] = []
    for hit in payload.get("results") or []:
        content = str(hit.get("content") or "").strip()
        if not content:
            continue
        name = hit.get("container_name") or "Membase"
        metadata = {k: v for k, v in hit.items() if k != "content" and v is not None}
        entries.append(MemoryEntry(content=f"[{name}] {content}", metadata=metadata or None))
    for c in _unavailable(payload):
        logger.warning("container=<%s>, reason=<%s> | Membase memory could not answer", c["name"], c["error"])
        entries.append(
            MemoryEntry(
                content=f"[{c['name']}] This Membase memory could not answer the search: {c['error']}",
                metadata={"container_name": c["name"], "container_id": c["id"], "error": c["error"]},
            )
        )
    return entries


def _unavailable(payload: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for c in payload.get("containers") or []:
        if c.get("error"):
            out.append(
                {"id": str(c.get("id") or ""), "name": str(c.get("name") or c.get("id")), "error": str(c["error"])}
            )
    return out


def _format_search(payload: dict[str, Any], query: str) -> str:
    hits = [h for h in payload.get("results") or [] if str(h.get("content") or "").strip()]
    lines = [
        f"{i}. [{h.get('container_name') or 'Membase'}] {str(h['content']).strip()}" for i, h in enumerate(hits, 1)
    ]
    errors = [f"- Memory '{c['name']}' could not answer: {c['error']}" for c in _unavailable(payload)]
    if not lines and not errors:
        return f"No Membase memory matched {query!r}."
    out = []
    if lines:
        out.append("Membase passages (cite the Memory in brackets):\n" + "\n".join(lines))
    if errors:
        out.append("Not searched, so missing results are not evidence of absence:\n" + "\n".join(errors))
    return "\n\n".join(out)


def _format_saved(result: dict[str, Any] | None) -> str:
    result = result or {}
    if result.get("static"):
        return "Saved to the user's Membase profile as a standing fact."
    return (
        "Saved to Membase. It is queued for learning and shows up in search once the Memory has "
        f"learned it (memory_id={result.get('memory_id')}, status={result.get('status', 'queued')})."
    )


def _format_profile(result: dict[str, Any]) -> str:
    sections = []
    for key, title in (("static", "Standing facts"), ("dynamic", "Recently changed"), ("results", "On this topic")):
        items = [_profile_item(x) for x in result.get(key) or []]
        items = [x for x in items if x]
        if items:
            sections.append(f"{title}:\n" + "\n".join(f"- {x}" for x in items))
    return "\n\n".join(sections) or "The Membase profile is empty."


def _profile_item(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("content") or item.get("fact") or item.get("text") or "").strip()
    return str(item).strip()


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
    except UnprocessableEntityError as error:
        return (
            "The user's Membase memory cannot run right now (no working model or agent container; "
            f"the owner can fix it under AI Setup): {error.message}"
        )
    except RateLimitError:
        return "Membase is busy for this account right now (rate limited); try again shortly."
    except MembaseError as error:
        return f"Membase error: {error}"
    except ValueError as error:  # an unknown Memory name
        return str(error)
