"""Membase memory for Haystack: a memory store, a retriever component and agent tools.

* :class:`MembaseMemoryStore` owns the Membase client. ``search_memories`` returns system
  ``ChatMessage`` objects, each prefixed with the Memory it came from in brackets, and
  ``add_memories`` saves the user's own words as one note.
* :class:`MembaseMemoryRetriever` is the pipeline read path. Its search is time-boxed (8 s by
  default) because the first Membase search after a quiet spell can take up to a minute while
  the memory wakes; when the budget runs out it returns no memories and the pipeline goes on
  (fail open), while the search keeps running so the memory is awake for the next run.
* :func:`membase_tools` gives a Haystack ``Agent`` ``membase_search``, ``membase_save`` and
  ``membase_profile``: deliberate calls with the SDK's full timeout that turn errors into a
  sentence the model can relay.

One Membase key (or one local directory) is one person's memory, so one store serves one person.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from haystack import component, default_from_dict, default_to_dict
from haystack.dataclasses import ChatMessage
from haystack.tools import Tool
from haystack.utils import Secret, deserialize_secrets_inplace
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

logger = logging.getLogger(__name__)

# The package's own default: it does not depend on which SDK release is installed.
DEFAULT_BASE_URL = "https://api.app.membase.com"
DEFAULT_RECALL_TIMEOUT = 8.0
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


class MembaseMemoryStore:
    """One person's Membase memory.

    :param api_key: A Membase developer key (Read, or Read & write to save). Defaults to the
        ``MEMBASE_API_KEY`` environment variable.
    :param base_url: Overrides ``https://api.app.membase.com`` (or ``MEMBASE_BASE_URL``).
    :param local: A directory for local mode (``pip install 'membase-haystack[local]'``).
    :param client: A ready :class:`membase.Membase` client, instead of the above. Not serialized.
    """

    def __init__(
        self,
        *,
        api_key: Secret = Secret.from_env_var("MEMBASE_API_KEY", strict=False),  # noqa: B008 - the Haystack idiom
        base_url: str | None = None,
        local: str | None = None,
        client: Membase | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.local = local
        self._client = client

    @property
    def client(self) -> Membase:
        """The Membase client, built on first use."""
        if self._client is None:
            base_url = self.base_url or os.environ.get("MEMBASE_BASE_URL") or DEFAULT_BASE_URL
            self._client = Membase(api_key=self.api_key.resolve_value(), base_url=base_url, local=self.local)
        return self._client

    def warm_up(self) -> None:
        """Build the client, so a missing key surfaces before the first run."""
        _ = self.client

    def search_memories(self, *, query: str, top_k: int = 5) -> list[ChatMessage]:
        """Passages on ``query`` as system messages, each prefixed with its Memory in brackets.

        A Memory that could not answer comes back as a message saying so, not as silence.
        """
        if not query or not query.strip():
            return []
        payload = self.client.search(query, limit=top_k)
        messages = []
        for hit in payload.get("items") or []:
            content = str(hit.get("content") or "").strip()
            if content:
                meta = {k: v for k, v in hit.items() if k != "content" and v is not None}
                messages.append(
                    ChatMessage.from_system(f"[{hit.get('memory_name') or 'Membase'}] {content}", meta=meta)
                )
        for name in _unavailable(payload):
            messages.append(
                ChatMessage.from_system(
                    f"[{name}] This Membase memory could not answer.", meta={"memory_name": name, "reason": "error"}
                )
            )
        return messages

    def add_memories(self, *, messages: list[ChatMessage], title: str = "") -> dict[str, Any] | None:
        """Save the user messages' text as one note in the key's write target.

        Only the user's own words are kept, so assistant answers are not learned as facts.
        Returns the Membase receipt, or ``None`` when there was nothing to save.
        """
        text = "\n\n".join(m.text.strip() for m in messages if m.is_from("user") and m.text and m.text.strip())
        if not text:
            return None
        return self.client.memories.add(text, title=title)

    def profile(self) -> list[str]:
        """The user's profile facts (name, role, lasting preferences), newest first."""
        result = self.client.profile_facts.list()
        return [s for s in (str(f.get("content") or "").strip() for f in result.get("items") or []) if s]

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, api_key=self.api_key.to_dict(), base_url=self.base_url, local=self.local)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MembaseMemoryStore:
        deserialize_secrets_inplace(data["init_parameters"], keys=["api_key"])
        return default_from_dict(cls, data)


@component
class MembaseMemoryRetriever:
    """Retrieves Membase memory as system ``ChatMessage`` objects for a pipeline.

    :param memory_store: The store to search.
    :param top_k: Passages per search.
    :param recall_timeout: Budget in seconds. When it runs out the component returns no
        memories and the pipeline goes on. ``None`` waits for the SDK's own timeout.
    """

    def __init__(
        self, *, memory_store: MembaseMemoryStore, top_k: int = 5, recall_timeout: float | None = DEFAULT_RECALL_TIMEOUT
    ) -> None:
        if recall_timeout is not None and recall_timeout <= 0:
            raise ValueError("MembaseMemoryRetriever: recall_timeout must be positive or None")
        self.memory_store = memory_store
        self.top_k = top_k
        self.recall_timeout = recall_timeout

    def warm_up(self) -> None:
        self.memory_store.warm_up()

    @component.output_types(memories=list[ChatMessage])
    def run(self, query: str, top_k: int | None = None) -> dict[str, list[ChatMessage]]:
        """Search Membase for ``query``.

        :param query: What to look for, usually the user's message.
        :param top_k: Overrides the init-time number of passages.
        :returns: ``memories``: the passages as system messages, or none when the budget ran out.
        """
        call = lambda: self.memory_store.search_memories(query=query, top_k=top_k or self.top_k)  # noqa: E731
        if self.recall_timeout is None:
            return {"memories": call()}
        return {"memories": _within(self.recall_timeout, call, default=[])}

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(
            self, memory_store=self.memory_store.to_dict(), top_k=self.top_k, recall_timeout=self.recall_timeout
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MembaseMemoryRetriever:
        data["init_parameters"]["memory_store"] = MembaseMemoryStore.from_dict(data["init_parameters"]["memory_store"])
        return default_from_dict(cls, data)


def membase_tools(memory_store: MembaseMemoryStore, *, tools: Iterable[str] = ALL_TOOLS) -> list[Tool]:
    """``membase_search``, ``membase_save`` and ``membase_profile`` for a Haystack ``Agent``.

    Drop ``"save"`` for a Read key, ``"profile"`` when the owner did not share it.
    """
    names = tuple(tools)
    unknown = sorted(set(names) - set(ALL_TOOLS))
    if unknown:
        raise ValueError(f"membase_tools: unknown tools {unknown}; choose from {ALL_TOOLS}")
    store = memory_store

    def search(query: str, limit: int | None = None) -> str:
        return _tool_call(lambda: _format_search(store.client.search(query, limit=limit), query))

    def save(content: str) -> str:
        if not content or not content.strip():
            return "Nothing to save: content is empty."
        return _tool_call(lambda: _format_saved(store.client.memories.add(content)))

    def profile() -> str:
        return _tool_call(lambda: _format_profile(store.profile()))

    available = {
        "search": Tool(
            name="membase_search",
            description=(
                "Search the user's Membase memory for what they told their assistants before and what their "
                "Memories have read. Each passage names the Memory it came from in [brackets]; cite it. Use it "
                "when the answer may depend on the user's past, preferences or documents. The first search after "
                "a quiet spell can take up to a minute."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look for, in plain words."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Maximum passages."},
                },
                "required": ["query"],
            },
            function=search,
        ),
        "save": Tool(
            name="membase_save",
            description=(
                "Save one fact or note to the user's Membase memory. Only when the user asked you to remember it "
                "or plainly meant it to be kept; save their words, not your summary of the conversation. Never "
                "save secrets, credentials or what other people said."
            ),
            parameters={
                "type": "object",
                "properties": {"content": {"type": "string", "description": "What to save, in the user's words."}},
                "required": ["content"],
            },
            function=save,
        ),
        "profile": Tool(
            name="membase_profile",
            description=(
                "Read the user's Membase profile: standing statements about who they are (name, role, lasting "
                "preferences), newest first. Call it once near the start of a conversation."
            ),
            parameters={"type": "object", "properties": {}},
            function=profile,
        ),
    }
    return [available[name] for name in ALL_TOOLS if name in names]


# ---------------------------------------------------------------------- helpers


def _within(seconds: float, call: Callable[[], Any], *, default: Any) -> Any:
    """Run a blocking call with a budget. On timeout it keeps running in a daemon thread and
    ``default`` is returned; errors other than the timeout propagate."""
    future: concurrent.futures.Future[Any] = concurrent.futures.Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(call())
        except BaseException as error:  # noqa: BLE001 - handed to the waiting caller
            future.set_exception(error)

    threading.Thread(target=run, name="membase-recall", daemon=True).start()
    try:
        return future.result(timeout=seconds)
    except concurrent.futures.TimeoutError:
        future.add_done_callback(lambda f: f.exception())  # keep a late error retrieved
        logger.warning("Membase recall took longer than %ss (memory may be waking up); going on without it", seconds)
        return default


def _unavailable(payload: Mapping[str, Any]) -> list[str]:
    """Memories that could not answer (``partial`` with ``reason: error``). A Memory whose
    passages only lost to ``limit`` (``truncated``) did answer."""
    return [str(p.get("memory_name") or "a Memory") for p in payload.get("partial") or [] if p.get("reason") == "error"]


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


def _format_profile(facts: list[str]) -> str:
    if not facts:
        return "The Membase profile is empty."
    return "About the user:\n" + "\n".join(f"- {f}" for f in facts)


def _tool_call(call: Callable[[], str]) -> str:
    """Run an SDK call for a tool and turn Membase errors into a sentence."""
    try:
        return call()
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
