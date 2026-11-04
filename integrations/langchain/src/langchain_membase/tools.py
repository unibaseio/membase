"""Agent tools: search, save (only when the user asked) and read the profile."""

from __future__ import annotations

import os
from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, BaseToolkit, ToolException
from membase import Membase
from membase.errors import MembaseError, PermissionDeniedError
from pydantic import BaseModel, Field

from ._common import (
    ClientFor,
    MembaseClientMixin,
    describe_error,
    format_search,
    user_id_from,
)


class _MembaseTool(MembaseClientMixin, BaseTool):
    handle_tool_error: bool = True
    """A Membase failure becomes the tool's answer, so the model can tell the user."""

    def _client_for_call(self, config: RunnableConfig | None) -> Membase:
        return self._membase(user_id_from(config))


class MembaseSearchInput(BaseModel):
    query: str = Field(description="What to look for, as a natural-language question or topic.")


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
    """How many passages in all, across every Memory the key reaches."""

    def _run(
        self,
        query: str,
        *,
        config: RunnableConfig,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> tuple[str, dict[str, Any]]:
        client = self._client_for_call(config)
        try:
            resp = client.search(query, limit=self.k)
        except MembaseError as e:
            raise ToolException(describe_error(e)) from e
        return format_search(resp), resp


class MembaseSaveInput(BaseModel):
    content: str = Field(
        description="The fact or note to keep, in the user's own words. One fact per call."
    )


class MembaseSaveTool(_MembaseTool):
    """Save one fact to the user's Membase memory. Writes only when the model calls it.

    Needs a Read & write key whose writes have one place to go: a key that reaches a single
    Memory saves there. There is deliberately no delete or forget tool.
    """

    name: str = "membase_save"
    description: str = (
        "Save one fact or note to the user's long-term memory (Membase). Call this ONLY when the "
        "user asks you to remember something or plainly means for it to be kept; never to log "
        "the conversation. Pass the content in the user's own words."
    )
    args_schema: type[BaseModel] = MembaseSaveInput

    def _run(
        self,
        content: str,
        *,
        config: RunnableConfig,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        client = self._client_for_call(config)
        try:
            resp = client.memories.add(content)
        except MembaseError as e:
            raise ToolException(describe_error(e)) from e
        return _saved(resp or {})


def _saved(resp: dict[str, Any]) -> str:
    learning = resp.get("learning") or []
    where = next((x.get("memory_name") for x in learning if x.get("memory_name")), None)
    where = where or "the user's memory"
    if resp.get("status") == "succeeded":
        return f"Saved to {where} and learned; a search can find it now."
    if resp.get("status") == "failed":
        return f"Saved to {where}, but learning it failed; a search may not find it."
    return (
        f"Saved to {where}. It is learned in the background, so a search may not find it until "
        "that finishes."
    )


class MembaseProfileInput(BaseModel):
    """The profile tool takes no arguments."""


class MembaseProfileTool(_MembaseTool):
    """Who the user is: the profile facts, standing statements from the Membase profile.

    Only works when the key's owner ticked the profile for it; otherwise Membase answers 403.
    """

    name: str = "membase_profile"
    description: str = (
        "Read who the user is from their Membase profile: standing statements (name, role, "
        "lasting preferences), newest first. Call it once near the start of a conversation."
    )
    args_schema: type[BaseModel] = MembaseProfileInput

    def _run(
        self,
        *,
        config: RunnableConfig,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        client = self._client_for_call(config)
        try:
            resp = client.profile_facts.list()
        except PermissionDeniedError as e:
            raise ToolException(
                describe_error(e) + " (A key reads the profile only if its owner ticked it.)"
            ) from e
        except MembaseError as e:
            raise ToolException(describe_error(e)) from e
        return _format_profile(resp or {})


def _format_profile(resp: dict[str, Any]) -> str:
    facts = [str(f.get("content") or "").strip() for f in resp.get("items") or []]
    facts = [f for f in facts if f]
    if not facts:
        return "The profile is empty."
    return "About the user:\n" + "\n".join(f"- {f}" for f in facts)


class MembaseToolkit(MembaseClientMixin, BaseToolkit):
    """The Membase tools, sharing one client (or one ``client_for``).

    .. code-block:: python

        from langchain_membase import MembaseToolkit

        tools = MembaseToolkit().get_tools()  # MEMBASE_API_KEY from the environment
    """

    k: int | None = Field(default=None, ge=1, le=50)
    """How many passages a search returns in all."""
    save: bool = True
    """Include ``membase_save`` (a Read key cannot write anyway)."""
    profile: bool = True
    """Include ``membase_profile`` (only useful if the key's owner ticked the profile)."""

    def get_tools(self) -> list[BaseTool]:
        shared: dict[str, Any] = {"client": self.client, "client_for": self.client_for}
        tools: list[BaseTool] = [MembaseSearchTool(**shared, k=self.k)]
        if self.save:
            tools.append(MembaseSaveTool(**shared))
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
        k=k,
        save=save,
        profile=profile,
    ).get_tools()
