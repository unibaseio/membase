"""MembaseRecallMiddleware: time-boxed recall before the model call of a ``create_agent`` agent.

Needs ``langchain>=1.1`` (``pip install 'langchain-membase[agents]'``).
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from membase import Membase

from ._common import (
    DEFAULT_RECALL_TIMEOUT,
    ClientFor,
    acall_with_budget,
    build_client,
    call_with_budget,
    describe_error,
    format_passages,
    logger,
    resolve_client,
    unavailable,
    user_id_from,
)

RECALL_HEADER = (
    "## From the user's memory (Membase)\n"
    "Passages that may be relevant to the user's latest message, each with the Memory it came "
    "from in [brackets]. Use them only if they help, and cite the Memory when you do."
)

_CACHE_SIZE = 512


class MembaseRecallMiddleware(AgentMiddleware):
    """Searches Membase once per user message and adds what it finds to the system message.

    * Recall is automatic, so it is time-boxed (``timeout``, 8 s by default) and fails open:
      on a timeout, a refused key or any other error it logs and the model runs without memory.
      A search that ran past the budget keeps going in the background, which wakes the user's
      memory for the next turn.
    * The search runs on the first model call after a user message; the model calls that follow
      tool calls in the same turn reuse its result.
    * The recall goes into the request's system message only; it is never written to the
      agent's state or checkpoint.
    * ``capture_turns=True`` (off by default) saves the user's own message(s) of each turn,
      once, as a note in ``container``; assistant replies are never saved. Writing is
      otherwise left to ``MembaseSaveTool``.

    Multi-user: pass ``client_for``; the user id comes from the run context
    (``context_schema`` with ``user_id``) or ``config["configurable"]["user_id"]``.
    """

    def __init__(
        self,
        client: Membase | None = None,
        *,
        client_for: ClientFor | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        local: str | os.PathLike | bool | None = None,
        container: str | None = None,
        k: int | None = 5,
        timeout: float = DEFAULT_RECALL_TIMEOUT,
        capture_turns: bool = False,
        header: str = RECALL_HEADER,
    ) -> None:
        super().__init__()
        if client_for is not None and (client is not None or api_key or local):
            raise ValueError("pass either client_for or client / api_key / local, not both")
        if client_for is None and client is None:
            client = build_client(api_key, base_url, local)
        self.client = client
        self.client_for = client_for
        self.container = container
        self.k = k
        self.timeout = timeout
        self.capture_turns = capture_turns
        self.header = header
        self._cache: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._lock = threading.Lock()

    # -- model call -------------------------------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        key = self._turn_key(request)
        if key is None:
            return handler(request)
        recall = self._cached(key)
        if recall is None:
            recall = self._recall(lambda: self._search(key[0], key[2]))
            self._remember(key, recall)
        return handler(self._with_recall(request, recall))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        key = self._turn_key(request)
        if key is None:
            return await handler(request)
        recall = self._cached(key)
        if recall is None:
            try:
                resp = await acall_with_budget(lambda: self._search(key[0], key[2]), self.timeout)
                recall = self._format(resp)
            except Exception as e:  # noqa: BLE001 - automatic recall fails open
                recall = self._failed(e)
            self._remember(key, recall)
        return await handler(self._with_recall(request, recall))

    # -- optional capture -------------------------------------------------------------------

    def after_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        if self.capture_turns:
            self._capture(state, runtime)
        return None

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        if self.capture_turns:
            self._capture(state, runtime)
        return None

    def _capture(self, state: Any, runtime: Any) -> None:
        """Save the user's own message(s) of the turn, once. Assistant replies are never saved:
        an AI answer must not be learned as a fact about the user."""
        messages: list[BaseMessage] = list(state.get("messages") or [])
        last_human = _last_index(messages, HumanMessage)
        if last_human is None:
            return
        first = last_human
        while first > 0 and isinstance(messages[first - 1], HumanMessage):
            first -= 1
        turn = messages[first : last_human + 1]
        note = "\n\n".join(t for t in (m.text.strip() for m in turn) if t)
        if not note:
            return
        user_id = _user_id(runtime)
        turn_key = (user_id or "", "captured:" + ",".join(m.id or str(hash(m.text)) for m in turn))
        with self._lock:
            if turn_key in self._cache:
                return
            self._cache[turn_key] = ""
        try:
            client = resolve_client(self.client, self.client_for, user_id)
        except Exception as e:  # noqa: BLE001 - capture never breaks the run
            logger.warning("Membase capture skipped: %s", describe_error(e))
            return

        def save() -> None:
            try:
                client.memories.add(note, container=self.container)
            except Exception as e:  # noqa: BLE001 - logged, the run already finished
                logger.warning("Membase capture failed: %s", describe_error(e))

        # Saving waits on the network (up to the SDK timeout); do not hold the agent's answer.
        threading.Thread(target=save, name="membase-capture", daemon=True).start()

    # -- helpers ----------------------------------------------------------------------------

    def _turn_key(self, request: ModelRequest) -> tuple[str, str, str] | None:
        """(user id, message id, query) of the latest user message, or None when there is none."""
        messages = list(request.messages or [])
        idx = _last_index(messages, HumanMessage)
        if idx is None:
            return None
        msg = messages[idx]
        query = msg.text.strip()
        if not query:
            return None
        return (_user_id(request.runtime) or "", msg.id or f"{idx}:{hash(query)}", query)

    def _search(self, user_id: str, query: str) -> dict[str, Any]:
        client = resolve_client(self.client, self.client_for, user_id or None)
        return client.search(query, container=self.container, limit=self.k)

    def _recall(self, fn: Callable[[], dict[str, Any]]) -> str:
        try:
            return self._format(call_with_budget(fn, self.timeout))
        except Exception as e:  # noqa: BLE001 - automatic recall fails open
            return self._failed(e)

    def _format(self, resp: dict[str, Any]) -> str:
        failed = unavailable(resp)
        if failed:
            logger.warning("Membase recall: some Memories could not answer: %s", "; ".join(failed))
        return format_passages(resp.get("results") or [])

    def _failed(self, e: BaseException) -> str:
        logger.warning("Membase recall skipped, continuing without memory: %s", describe_error(e))
        return ""

    def _cached(self, key: tuple[str, str, str]) -> str | None:
        with self._lock:
            return self._cache.get(key[:2])

    def _remember(self, key: tuple[str, str, str], recall: str) -> None:
        with self._lock:
            self._cache[key[:2]] = recall
            self._cache.move_to_end(key[:2])
            while len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)

    def _with_recall(self, request: ModelRequest, recall: str) -> ModelRequest:
        if not recall:
            return request
        block = f"{self.header}\n{recall}"
        current = request.system_message
        if current is None:
            return request.override(system_message=SystemMessage(content=block))
        if isinstance(current.content, str):
            content: Any = f"{current.content}\n\n{block}" if current.content else block
        else:
            content = [*current.content, {"type": "text", "text": block}]
        return request.override(system_message=SystemMessage(content=content))


def _user_id(runtime: Any) -> str | None:
    """From the run context (``context_schema`` with ``user_id``), else ``configurable``."""
    user_id = user_id_from(None, getattr(runtime, "context", None))
    if user_id is None:
        try:
            from langgraph.config import get_config

            user_id = user_id_from(get_config())
        except Exception:  # noqa: BLE001 - outside a run, or Python 3.10 async
            user_id = None
    return user_id


def _last_index(messages: list[BaseMessage], kind: type[BaseMessage]) -> int | None:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], kind):
            return i
    return None
