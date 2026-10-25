"""Shared plumbing: which client serves a call, how errors read, how recall is formatted."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from membase import Membase
from membase.errors import (
    APIConnectionError,
    APITimeoutError,
    MembaseError,
    PermissionDeniedError,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

logger = logging.getLogger("langchain_membase")

ClientFor = Callable[[str], Membase]
"""Returns the Membase client of one end user: their own key, or their own local directory."""

T = TypeVar("T")

# The package's own default: it does not depend on which SDK release is installed.
DEFAULT_BASE_URL = "https://api.app.membase.com"

DEFAULT_RECALL_TIMEOUT = 8.0
"""Budget, in seconds, for automatic per-turn recall. Explicit tool calls use the SDK's own."""


class MissingUserIdError(ValueError):
    """``client_for`` is set but the call carried no user id, so no client may serve it."""


def build_client(
    api_key: str | SecretStr | None = None,
    base_url: str | None = None,
    local: str | os.PathLike | bool | None = None,
) -> Membase:
    """A Membase client from the usual arguments, falling back to ``MEMBASE_API_KEY``."""
    key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
    base_url = base_url or os.environ.get("MEMBASE_BASE_URL") or DEFAULT_BASE_URL
    return Membase(api_key=key, base_url=base_url, local=local)


class MembaseClientMixin(BaseModel):
    """Fields every Membase object takes: one ready client, or ``client_for`` for many users."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: Membase | None = Field(default=None, exclude=True, repr=False)
    """A ready Membase client. One client serves every call: right for a personal agent."""
    client_for: ClientFor | None = Field(default=None, exclude=True, repr=False)
    """``user_id -> Membase`` for a multi-user app; the user id comes from the run's config."""
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    """Membase API key; defaults to ``MEMBASE_API_KEY``. Used when no ``client`` is given."""
    base_url: str | None = None
    """API base URL; defaults to ``MEMBASE_BASE_URL`` or ``https://api.app.membase.com``."""
    local: str | Path | bool | None = Field(default=None, exclude=True)
    """Run the Membase API in-process on this directory (``True`` = ``~/.membase``)."""

    @model_validator(mode="after")
    def _one_way_to_reach_memory(self) -> MembaseClientMixin:
        if self.client_for is not None:
            if self.client is not None or self.api_key is not None or self.local:
                raise ValueError(
                    "pass either client_for (one client per user) or client / api_key / local "
                    "(one client for every call), not both"
                )
            return self
        if self.client is None:
            self.client = build_client(self.api_key, self.base_url, self.local or None)
        return self

    def _membase(self, user_id: str | None) -> Membase:
        return resolve_client(self.client, self.client_for, user_id)


def resolve_client(
    client: Membase | None, client_for: ClientFor | None, user_id: str | None
) -> Membase:
    """The client for this call. With ``client_for`` a user id is mandatory: no fallback client."""
    if client_for is not None:
        if not user_id:
            raise MissingUserIdError(
                "client_for is set, so every call needs the end user's id: pass "
                "config={'configurable': {'user_id': ...}} (or a LangGraph context with user_id)"
            )
        return client_for(user_id)
    if client is None:  # pragma: no cover - the validator always sets one
        raise ValueError("no Membase client configured")
    if user_id:
        _warn_single_client_once()
    return client


_warned_single_client = False


def _warn_single_client_once() -> None:
    global _warned_single_client
    if not _warned_single_client:
        _warned_single_client = True
        logger.warning(
            "a user_id reached langchain-membase but no client_for is set, so one Membase key "
            "serves every user. That suits a personal agent only; a multi-user app must pass "
            "client_for so each person's call uses their own key."
        )


def user_id_from(config: Mapping[str, Any] | None = None, context: Any = None) -> str | None:
    """The end user's id: ``configurable.user_id`` first, then a LangGraph runtime context."""
    configurable = (config or {}).get("configurable") or {}
    uid = configurable.get("user_id")
    if uid in (None, "") and context is None:
        context = _langgraph_context()
    if uid in (None, "") and context is not None:
        uid = (
            context.get("user_id")
            if isinstance(context, Mapping)
            else getattr(context, "user_id", None)
        )
    return str(uid) if uid not in (None, "") else None


def _langgraph_context() -> Any:
    try:
        from langgraph.runtime import get_runtime
    except ImportError:
        return None
    try:
        return get_runtime().context
    except Exception:  # noqa: BLE001 - outside a graph run there is simply no context
        return None


def describe_error(e: BaseException) -> str:
    """One actionable sentence for the model and the logs."""
    if isinstance(e, PermissionDeniedError):
        return (
            f"Membase refused this request (403): {e.message}. This Membase key was narrowed or "
            "revoked by its owner (a Memory switched off, an access level too low for this "
            "action, or the key revoked); do not retry with this key."
        )
    if isinstance(e, APITimeoutError):
        return (
            "Membase did not answer in time; the user's memory may still be waking up after a "
            "quiet spell. Try again in a moment."
        )
    if isinstance(e, APIConnectionError):
        return f"Could not reach Membase: {e.message}."
    if isinstance(e, MembaseError):
        return f"Membase error: {e}"
    return f"{type(e).__name__}: {e}"


def unavailable(resp: Mapping[str, Any]) -> list[str]:
    """``name: error`` for every Memory that could not answer a search."""
    out = []
    for c in resp.get("containers") or []:
        if c.get("error"):
            out.append(f"{c.get('name') or c.get('id')}: {c['error']}")
    return out


def format_passages(results: list[Mapping[str, Any]]) -> str:
    """One line per passage, each citing the Memory (container) it came from."""
    lines = []
    for r in results:
        name = r.get("container_name") or r.get("container") or "Membase"
        content = str(r.get("content") or "").strip()
        if content:
            lines.append(f"- [{name}] {content}")
    return "\n".join(lines)


def format_search(resp: Mapping[str, Any]) -> str:
    """Passages with their Memory, plus any Memory that could not answer (never 'no memory')."""
    body = format_passages(resp.get("results") or [])
    failed = unavailable(resp)
    note = ""
    if failed:
        note = "These Memories could not answer, so what they hold is unknown: " + "; ".join(failed)
    if body:
        return body + (f"\n\n{note}" if note else "")
    if note:
        return "No passages came back. " + note
    return "No matching memories."


def call_with_budget(fn: Callable[[], T], timeout: float) -> T:
    """Run ``fn`` but stop waiting after ``timeout`` seconds (raises ``TimeoutError``).

    The call keeps running on a daemon thread; letting it finish wakes the user's memory, so
    the next turn is fast. A daemon thread never holds up interpreter exit.
    """
    box: dict[str, Any] = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = e
        finally:
            done.set()

    threading.Thread(target=run, name="membase-recall", daemon=True).start()
    if not done.wait(timeout):
        raise TimeoutError(f"Membase recall took longer than {timeout:g}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


async def acall_with_budget(fn: Callable[[], T], timeout: float) -> T:
    """``call_with_budget`` for an event loop: the blocking SDK call runs off the loop."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[T] = loop.create_future()

    def settle(value: Any = None, error: BaseException | None = None) -> None:
        if fut.done():
            return
        if error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(value)

    def run() -> None:
        try:
            value, error = fn(), None
        except BaseException as e:  # noqa: BLE001 - handed to the awaiting coroutine
            value, error = None, e
        try:
            loop.call_soon_threadsafe(settle, value, error)
        except RuntimeError:  # the loop closed while the call was still running
            pass

    threading.Thread(target=run, name="membase-recall", daemon=True).start()
    try:
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Membase recall took longer than {timeout:g}s") from None
