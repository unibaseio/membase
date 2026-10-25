"""``membase_tools``: search, save and profile as Agent Framework function tools."""

from __future__ import annotations

import os
from typing import Annotated, Any

from agent_framework import FunctionInvocationContext, FunctionTool, tool
from membase import Membase
from membase.errors import BadRequestError, MembaseError, PermissionDeniedError
from pydantic import Field

from ._common import (
    DEFAULT_SOURCE_ID,
    REVOKED,
    ClientFor,
    Clients,
    NoUserBound,
    describe_error,
    format_search,
    in_thread,
)


def _resolve_container(client: Membase, wanted: str) -> tuple[str | None, str]:
    """``(container_id, name)`` for a container given by id or by name, else ``(None, message)``."""
    containers = client.containers.list().get("containers") or []
    key = wanted.strip().casefold()
    for c in containers:
        if str(c.get("id", "")) == wanted.strip():
            return str(c["id"]), str(c.get("name") or c["id"])
    for c in containers:
        if str(c.get("name", "")).strip().casefold() == key:
            return str(c["id"]), str(c.get("name") or c["id"])
    names = ", ".join(f'"{c.get("name")}"' for c in containers) or "none"
    return None, f'No Memory called "{wanted}" is in reach of this key. Memories in reach: {names}.'


def _names(client: Membase) -> str:
    try:
        containers = client.containers.list().get("containers") or []
    except MembaseError:
        return ""
    return ", ".join(f'"{c.get("name")}"' for c in containers)


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
        container: Annotated[
            str | None,
            Field(
                description="Only this Memory (its name as shown in results). Omit to search every Memory."
            ),
        ] = None,
        limit: Annotated[int | None, Field(description="How many passages, 1-50.", ge=1, le=50)] = None,
        ctx: FunctionInvocationContext | None = None,
    ) -> str:
        """Search the user's long-term Membase memory. Returns passages, each tagged with the
        Memory it came from; name that Memory when you use a passage."""
        try:
            mb = _client(ctx)
            container_id = None
            if container:
                container_id, note = await in_thread(_resolve_container, mb, container)
                if container_id is None:
                    return note
            answer = await in_thread(mb.search, query, container=container_id, limit=limit)
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
            out += ["These Memories could not answer, so this is not proof nothing is stored:", *unanswered]
        return "\n".join(out)

    @tool(name="membase_save", approval_mode="never_require")
    async def membase_save(
        content: Annotated[
            str, Field(description="The fact to keep, in the user's own words. One fact per call.")
        ],
        container: Annotated[
            str | None,
            Field(description="The Memory that keeps it (its name). Needed when the user has several."),
        ] = None,
        about_user: Annotated[
            bool,
            Field(
                description=(
                    "True for a standing fact about the user themselves (who they are, a lasting "
                    "preference): it goes to their profile instead of a Memory."
                )
            ),
        ] = False,
        ctx: FunctionInvocationContext | None = None,
    ) -> str:
        """Save something to the user's Membase memory. Call this only when the user asks you
        to remember something or plainly means it; never save your own conclusions."""
        try:
            mb = _client(ctx)
            container_id, name = None, ""
            if container and not about_user:
                container_id, name = await in_thread(_resolve_container, mb, container)
                if container_id is None:
                    return name
            result = await in_thread(mb.memories.add, content, container=container_id, static=about_user)
        except NoUserBound as exc:
            return f"Membase is not available: {exc}."
        except BadRequestError as exc:
            names = await in_thread(_names, mb)
            hint = f" Pass container= with one of: {names}." if names and not container else ""
            return f"Membase did not save it: {exc.message}.{hint}"
        except PermissionDeniedError as exc:
            return f"Membase did not save it (403): {REVOKED}; a Read key cannot write. ({exc.message})"
        except Exception as exc:  # noqa: BLE001 - the tool answers the model with the error
            return describe_error(exc)
        if (result or {}).get("static"):
            return "Recorded in the user's Membase profile."
        where = f' "{name}"' if name else ""
        return f"Saved to Membase{where}; it is queued and will be searchable once the Memory has learned it."

    @tool(name="membase_profile", approval_mode="never_require")
    async def membase_profile(
        topic: Annotated[
            str | None, Field(description="Optional topic to pull relevant profile facts for.")
        ] = None,
        ctx: FunctionInvocationContext | None = None,
    ) -> str:
        """Read the user's Membase profile: standing facts about who they are and their
        preferences, and the most recently changed ones. Read it once, near the start."""
        try:
            mb = _client(ctx)
            p = await in_thread(mb.profile, topic)
        except NoUserBound as exc:
            return f"Membase is not available: {exc}."
        except PermissionDeniedError as exc:
            return (
                "Membase refused the profile (403): this key's owner did not share the profile "
                f"with it, or {REVOKED}. ({exc.message})"
            )
        except Exception as exc:  # noqa: BLE001 - the tool answers the model with the error
            return describe_error(exc)
        out: list[str] = []
        for label, key in (
            ("Standing facts", "static"),
            ("Recently changed", "dynamic"),
            ("On the topic", "results"),
        ):
            items = [_text(x) for x in (p or {}).get(key) or []]
            items = [x for x in items if x]
            if items:
                out.append(f"{label}:\n" + "\n".join(f"- {x}" for x in items))
        return "\n\n".join(out) or "The user's Membase profile is empty."

    tools: list[FunctionTool] = [membase_search]
    if save:
        tools.append(membase_save)
    if profile:
        tools.append(membase_profile)
    return tools


def _text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("content") or item.get("text") or item.get("memory") or "").strip()
    return str(item).strip()


__all__ = ["membase_tools"]
