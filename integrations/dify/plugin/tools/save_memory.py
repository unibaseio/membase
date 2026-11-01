"""save_memory: one fact into the Membase memory, only when the model is asked to save it."""

from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from membase.errors import MembaseError

from membase_dify.client import MembaseToolError, explain, open_client


class SaveMemoryTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        content = str(tool_parameters.get("content") or "").strip()
        if not content:
            raise MembaseToolError("content is required: pass the fact to save.")
        try:
            with open_client(self.runtime.credentials) as client:
                # No request names a Memory: the fact lands in the key's write target.
                saved = client.memories.add(content)
        except MembaseError as err:
            raise MembaseToolError(explain(err)) from err

        saved = dict(saved or {})
        names = [
            str(x.get("memory_name")) for x in saved.get("learning") or [] if x.get("memory_name")
        ]
        where = ", ".join(names) if names else "the key's Memory"
        status = saved.get("status") or "pending"
        if status == "succeeded":
            text = f"Saved to {where} (status: succeeded). Search finds it now."
        else:
            text = (
                f"Saved to {where} (status: {status}). "
                "Search finds it once Membase has learned it, which happens in the background."
            )
        yield self.create_text_message(text)
        yield self.create_json_message(saved)
