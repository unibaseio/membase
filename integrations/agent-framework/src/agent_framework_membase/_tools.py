"""``membase_tools``: search, save and profile as Agent Framework function tools."""

from __future__ import annotations

import os
from typing import Annotated

from agent_framework import FunctionInvocationContext, FunctionTool, tool
from membase import Membase
from membase.errors import PermissionDeniedError, WriteTargetRequiredError
from pydantic import Field

from ._common import (
    DEFAULT_SOURCE_ID,
    NO_WRITE_TARGET,
    REVOKED,
    ClientFor,
    Clients,
    NoUserBound,
    describe_error,
    format_search,
    in_thread,
)


def membase_tools(
    client: Membase | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    local: str | os.PathLike[str] | bool | None = None,
    client_for: ClientFor | None = None,
    source_id: str = DEFAULT_SOURCE_ID,
    save: bool = True,
    profile: bool = True,
) -> list[FunctionTool]:
    """Membase as Agent Framework function tools: ``membase_search``, ``membase_save``,
    ``membase_profile``.

    Pass a ready ``Membase`` client, or ``api_key`` / ``base_url`` / ``local``, or
    ``client_for`` (``user_id -> Membase``) with the user bound to each session by
    :func:`bind_user` (the same binding :class:`MembaseContextProvider` reads). Tool calls use
    the SDK's own timeout (90 s by default), since the model asked for them explicitly.

    ``save=False`` leaves out ``membase_save`` (for a Read key); ``profile=False`` leaves out
    ``membase_profile`` (for a key whose owner did not share the profile). There is no delete
    or forget tool.
    """
    clients = Clients(
        client, api_key=api_key, base_url=base_url, local=local, client_for=client_for, source_id=source_id
    )

    def _client(ctx: FunctionInvocationContext | None) -> Membase:
        return clients.for_session(ctx.session if ctx is not None else None)

    @tool(name="membase_search", approval_mode="never_require")
    async def membase_search(
        query: Annotated[str, Field(description="What to look for, in natural language.")],
        limit: Annotated[int | None, Field(description="How many passages, 1-50.", ge=1, le=50)] = None,
        ctx: FunctionInvocationContext | None = None,
    ) -> str:
        """Search the user's long-term Membase memory (every Memory the key reaches). Returns
        passages, each tagged with the Memory it came from; name that Memory when you use a
        passage."""
        try:
            mb = _client(ctx)
            answer = await in_thread(mb.search, query, limit=limit)
        except NoUserBound as exc:
            return f"Membase is not available: {exc}."
        except Exception as exc:  # noqa: BLE001 - the tool answers the model with the error
            return describe_error(exc)
        passages, unanswered = format_search(answer)
        out = (
            ["Passages from the user's Membase memory:", *passages]
            if passages
            else ["No passages in the user's Membase memory matched."]
        )
        if unanswered:
            out += [
                "Some Memories did not fully answer, so this is not proof nothing is stored:",
                *unanswered,
            ]
        return "\n".join(out)

    @tool(name="membase_save", approval_mode="never_require")
    async def membase_save(
        content: Annotated[
            str, Field(description="The fact to keep, in the user's own words. One fact per call.")
        ],
        ctx: FunctionInvocationContext | None = None,
    ) -> str:
        """Save something to the user's Membase memory. Call this only when the user asks you
        to remember something or plainly means it; never save your own conclusions."""
        try:
            mb = _client(ctx)
            result = await in_thread(mb.memories.add, content)
        except NoUserBound as exc:
            return f"Membase is not available: {exc}."
        except WriteTargetRequiredError:
            return f"Membase did not save it: {NO_WRITE_TARGET}."
        except PermissionDeniedError as exc:
            return f"Membase did not save it (403): {REVOKED}; a Read key cannot write. ({exc.message})"
        except Exception as exc:  # noqa: BLE001 - the tool answers the model with the error
            return describe_error(exc)
        result = result or {}
        names = [x.get("memory_name") for x in result.get("learning") or [] if x.get("memory_name")]
        where = f' "{names[0]}"' if names else ""
        if result.get("status") == "succeeded":
            return f"Saved to Membase{where}; it is learned and searchable now."
        return f"Saved to Membase{where}; it will be searchable once the Memory has learned it."

    @tool(name="membase_profile", approval_mode="never_require")
    async def membase_profile(ctx: FunctionInvocationContext | None = None) -> str:
        """Read the user's Membase profile: standing statements about who they are (name, role,
        lasting preferences), newest first. Read it once, near the start."""
        try:
            mb = _client(ctx)
            p = await in_thread(mb.profile_facts.list)
        except NoUserBound as exc:
            return f"Membase is not available: {exc}."
        except PermissionDeniedError as exc:
            return (
                "Membase refused the profile (403): this key's owner did not share the profile "
                f"with it, or {REVOKED}. ({exc.message})"
            )
        except Exception as exc:  # noqa: BLE001 - the tool answers the model with the error
            return describe_error(exc)
        items = [str(x.get("content") or "").strip() for x in (p or {}).get("items") or []]
        items = [x for x in items if x]
        if not items:
            return "The user's Membase profile is empty."
        return "About the user:\n" + "\n".join(f"- {x}" for x in items)

    tools: list[FunctionTool] = [membase_search]
    if save:
        tools.append(membase_save)
    if profile:
        tools.append(membase_profile)
    return tools


__all__ = ["membase_tools"]
