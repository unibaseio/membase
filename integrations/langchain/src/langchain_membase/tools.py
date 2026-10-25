"""Agent tools: search, save (only when the user asked) and read the profile."""

from __future__ import annotations

import os
from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, BaseToolkit, ToolException
from membase import Membase
from membase.errors import BadRequestError, MembaseError, PermissionDeniedError
from pydantic import BaseModel, Field

from ._common import (
    ClientFor,
    MembaseClientMixin,
    describe_error,
    format_passages,
    format_search,
    user_id_from,
)


class _MembaseTool(MembaseClientMixin, BaseTool):
    handle_tool_error: bool = True
    """A Membase failure becomes the tool's answer, so the model can tell the user."""

    def _client_for_call(self, config: RunnableConfig | None) -> Membase:
        return self._membase(user_id_from(config))


def _container_id(client: Membase, ref: str) -> str:
    """A container id from a name or id the model gave, or a ToolException naming the choices."""
    containers = client.containers.list().get("containers") or []
    for c in containers:
        if c.get("id") == ref:
            return ref
    wanted = ref.strip().casefold()
    matches = [c for c in containers if str(c.get("name") or "").strip().casefold() == wanted]
    if len(matches) == 1:
        return str(matches[0]["id"])
    names = ", ".join(repr(c.get("name")) for c in containers) or "none"
    raise ToolException(f"No single Memory called {ref!r} is in reach. Memories in reach: {names}.")


class MembaseSearchInput(BaseModel):
    query: str = Field(description="What to look for, as a natural-language question or topic.")
    container: str | None = Field(
        default=None,
        description="Name of one Memory to search. Omit to search every Memory in reach.",
    )


class MembaseSearchTool(_MembaseTool):
    """Search the user's Membase memory; each passage names the Memory it came from.

    The tool answers with text for the model and keeps the raw search response as the
    ``ToolMessage`` artifact.
    """

    name: str = "membase_search"
    description: str = (
        "Search the user's long-term memory (Membase) for passages relevant to a question: what "
        "they told you before, their projects, decisions, preferences and documents. Each passage "
        "starts with the name of the Memory it came from in [brackets]; cite it in your answer. "
        "The first search after a quiet spell can take up to a minute."
    )
    args_schema: type[BaseModel] = MembaseSearchInput
    response_format: Literal["content", "content_and_artifact"] = "content_and_artifact"

    k: int | None = Field(default=None, ge=1, le=50)
    """How many passages in all; unset lets Membase cover every Memory in reach."""
    container: str | None = None
    """Default container id when the model names none; unset searches every Memory."""

    def _run(
        self,
        query: str,
        container: str | None = None,
        *,
        config: RunnableConfig,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> tuple[str, dict[str, Any]]:
        client = self._client_for_call(config)
        try:
            target = _container_id(client, container) if container else self.container
            resp = client.search(query, container=target, limit=self.k)
        except MembaseError as e:
            raise ToolException(describe_error(e)) from e
        return format_search(resp), resp


class MembaseSaveInput(BaseModel):
    content: str = Field(
        description="The fact or note to keep, in the user's own words. One fact per call."
    )
    static: bool = Field(
        default=False,
        description=(
            "True for a lasting fact about the user themself (name, role, a standing "
            "preference): it goes to their profile. False for a note about anything else."
        ),
    )
    container: str | None = Field(
        default=None,
        description="Name of the Memory that should keep a note (ignored when static is true).",
    )


class MembaseSaveTool(_MembaseTool):
    """Save one fact to the user's Membase memory. Writes only when the model calls it.

    Needs a Read & write key. There is deliberately no delete or forget tool.
    """

    name: str = "membase_save"
    description: str = (
        "Save one fact or note to the user's long-term memory (Membase). Call this ONLY when the "
        "user asks you to remember something or plainly means for it to be kept; never to log "
        "the conversation. Pass the content in the user's own words. Set static=true for a "
        "lasting fact about the user themself; otherwise it is a note kept by one Memory."
    )
    args_schema: type[BaseModel] = MembaseSaveInput

    container: str | None = None
    """Container id that keeps notes when the model names none. Needed when the key reaches
    several Memories (unless the model names one)."""

    def _run(
        self,
        content: str,
        static: bool = False,
        container: str | None = None,
        *,
        config: RunnableConfig,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        client = self._client_for_call(config)
        try:
            target = None
            if not static:
                target = _container_id(client, container) if container else self.container
            resp = client.memories.add(content, container=target, static=static)
        except BadRequestError as e:
            if not static and target is None:
                raise ToolException(_which_container(client, e)) from e
            raise ToolException(describe_error(e)) from e
        except MembaseError as e:
            raise ToolException(describe_error(e)) from e
        return _saved(resp or {})


def _which_container(client: Membase, e: BadRequestError) -> str:
    try:
        names = [c.get("name") for c in client.containers.list().get("containers") or []]
    except MembaseError:
        names = []
    listed = ", ".join(repr(n) for n in names) or "unknown"
    return (
        f"{e.message}. Several Memories are in reach; call again with container set to one "
        f"of: {listed}."
    )


def _saved(resp: dict[str, Any]) -> str:
    if resp.get("static"):
        return "Recorded in the user's profile."
    if resp.get("status") == "learned":
        return "Saved and learned; a search can find it now."
    where = resp.get("container_name") or resp.get("container") or "the Memory"
    return (
        f"Saved to {where}. It is learned in the background, so a search may not find it until "
        "that finishes."
    )


class MembaseProfileInput(BaseModel):
    topic: str | None = Field(
        default=None,
        description="Optional current topic, to also get the profile passages relevant to it.",
    )


class MembaseProfileTool(_MembaseTool):
    """Who the user is: standing facts and recently changed ones, from the Membase profile.

    Only works when the key's owner ticked the profile for it; otherwise Membase answers 403.
    """

    name: str = "membase_profile"
    description: str = (
        "Read who the user is from their Membase profile: standing facts (name, role, lasting "
        "preferences) and recently changed facts. Call it once near the start of a conversation, "
        "optionally with the current topic."
    )
    args_schema: type[BaseModel] = MembaseProfileInput

    def _run(
        self,
        topic: str | None = None,
        *,
        config: RunnableConfig,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        client = self._client_for_call(config)
        try:
            resp = client.profile(q=topic or None)
        except PermissionDeniedError as e:
            raise ToolException(
                describe_error(e) + " (A key reads the profile only if its owner ticked it.)"
            ) from e
        except MembaseError as e:
            raise ToolException(describe_error(e)) from e
        return _format_profile(resp or {})


def _text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("content") or item.get("description") or item.get("text") or "").strip()
    return str(item).strip()


def _format_profile(resp: dict[str, Any]) -> str:
    parts = []
    static = [t for t in map(_text, resp.get("static") or []) if t]
    dynamic = [t for t in map(_text, resp.get("dynamic") or []) if t]
    if static:
        parts.append("Standing facts:\n" + "\n".join(f"- {t}" for t in static))
    if dynamic:
        parts.append("Recently changed:\n" + "\n".join(f"- {t}" for t in dynamic))
    if resp.get("results"):
        passages = format_passages(resp["results"])
        if passages:
            parts.append("Relevant to the topic:\n" + passages)
    return "\n\n".join(parts) or "The profile is empty."


class MembaseToolkit(MembaseClientMixin, BaseToolkit):
    """The Membase tools, sharing one client (or one ``client_for``).

    .. code-block:: python

        from langchain_membase import MembaseToolkit

        tools = MembaseToolkit().get_tools()  # MEMBASE_API_KEY from the environment
    """

    container: str | None = None
    """Default container id for search and save when the model names none."""
    k: int | None = Field(default=None, ge=1, le=50)
    """How many passages a search returns in all."""
    save: bool = True
    """Include ``membase_save`` (a Read key cannot write anyway)."""
    profile: bool = True
    """Include ``membase_profile`` (only useful if the key's owner ticked the profile)."""

    def get_tools(self) -> list[BaseTool]:
        shared: dict[str, Any] = {"client": self.client, "client_for": self.client_for}
        tools: list[BaseTool] = [MembaseSearchTool(**shared, container=self.container, k=self.k)]
        if self.save:
            tools.append(MembaseSaveTool(**shared, container=self.container))
        if self.profile:
            tools.append(MembaseProfileTool(**shared))
        return tools


def membase_tools(
    client: Membase | None = None,
    *,
    client_for: ClientFor | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    local: str | os.PathLike | bool | None = None,
    container: str | None = None,
    k: int | None = None,
    save: bool = True,
    profile: bool = True,
) -> list[BaseTool]:
    """``MembaseToolkit(...).get_tools()``: search, save and profile sharing one client.

    ``save=False`` leaves the write tool out; ``profile=False`` leaves the profile out.
    """
    return MembaseToolkit(
        client=client,
        client_for=client_for,
        api_key=api_key,
        base_url=base_url,
        local=os.fspath(local) if isinstance(local, os.PathLike) else local,
        container=container,
        k=k,
        save=save,
        profile=profile,
    ).get_tools()
