"""save_memory: one fact into the Membase memory, only when the model is asked to save it."""

from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from membase.errors import BadRequestError, MembaseError

from membase_dify.client import (
    MembaseToolError,
    container_names,
    explain,
    open_client,
    resolve_container,
)


def _flag(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"true", "1", "yes"}


class SaveMemoryTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        content = str(tool_parameters.get("content") or "").strip()
        if not content:
            raise MembaseToolError("content is required: pass the fact to save.")
        static = _flag(tool_parameters.get("static"))
        try:
            with open_client(self.runtime.credentials) as client:
                target = (
                    None if static else resolve_container(client, tool_parameters.get("container"))
                )
                try:
                    saved = client.memories.add(
                        content, container=target["id"] if target else None, static=static
                    )
                except BadRequestError as err:
                    if static or target:
                        raise
                    # Several containers in reach and none named: say which ones exist.
                    containers = client.containers.list().get("containers") or []
                    raise MembaseToolError(
                        f"{explain(err)} Pass container as one of: {container_names(containers)}."
                    ) from err
        except MembaseError as err:
            raise MembaseToolError(explain(err)) from err

        saved = dict(saved or {})
        if static:
            text = "Saved to the user's profile as a standing fact."
        else:
            where = target.get("name") if target else "the key's container"
            saved.setdefault("container_name", target.get("name") if target else None)
            text = (
                f"Saved to {where} (status: {saved.get('status') or 'accepted'}). "
                "Search finds it once Membase has learned it, which happens in the background."
            )
        yield self.create_text_message(text)
        yield self.create_json_message(saved)
