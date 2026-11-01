"""search_memory: passages from the Membase memory, each citing the Memory it came from."""

from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from membase.errors import MembaseError

from membase_dify.client import MembaseToolError, explain, open_client


def _limit(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return max(1, min(int(float(raw)), 50))
    except (TypeError, ValueError) as err:
        raise MembaseToolError(f"limit must be a number from 1 to 50, not {raw!r}") from err


def format_results(items: list[dict], unavailable: list[dict]) -> str:
    lines = [
        f"{i}. [{r.get('memory_name') or 'Membase'}] {str(r.get('content') or '').strip()}"
        for i, r in enumerate(items, 1)
    ]
    if not lines:
        lines.append("No memories matched.")
    for p in unavailable:
        lines.append(
            f"Could not search the Memory '{p.get('memory_name') or 'unnamed'}'. "
            "It may hold an answer; this is not the same as no match."
        )
    return "\n".join(lines)


class SearchMemoryTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        query = str(tool_parameters.get("query") or "").strip()
        if not query:
            raise MembaseToolError("query is required: say what to look for.")
        limit = _limit(tool_parameters.get("limit"))
        try:
            with open_client(self.runtime.credentials) as client:
                answer = client.search(query, limit=limit)
        except MembaseError as err:
            raise MembaseToolError(explain(err)) from err

        items = list(answer.get("items") or [])
        partial = list(answer.get("partial") or [])
        # A Memory whose passages only lost to the limit ("truncated") did answer.
        unavailable = [p for p in partial if p.get("reason") == "error"]
        yield self.create_text_message(format_results(items, unavailable))
        yield self.create_json_message({"query": query, "items": items, "partial": partial})
