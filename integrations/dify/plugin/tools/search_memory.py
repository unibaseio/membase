"""search_memory: passages from the Membase memory, each citing its container."""

from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from membase.errors import MembaseError

from membase_dify.client import MembaseToolError, explain, open_client, resolve_container


def _limit(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return max(1, min(int(float(raw)), 50))
    except (TypeError, ValueError) as err:
        raise MembaseToolError(f"limit must be a number from 1 to 50, not {raw!r}") from err


def format_results(results: list[dict], unavailable: list[dict]) -> str:
    lines = [
        f"{i}. [{r.get('container_name') or 'Membase'}] {str(r.get('content') or '').strip()}"
        for i, r in enumerate(results, 1)
    ]
    if not lines:
        lines.append("No memories matched.")
    for c in unavailable:
        lines.append(
            f"Could not search container '{c.get('name') or c.get('id')}': {c.get('error')}. "
            "Its memory may hold an answer; this is not the same as no match."
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
                target = resolve_container(client, tool_parameters.get("container"))
                answer = client.search(
                    query, container=target["id"] if target else None, limit=limit
                )
        except MembaseError as err:
            raise MembaseToolError(explain(err)) from err

        results = list(answer.get("results") or [])
        containers = list(answer.get("containers") or [])
        unavailable = [c for c in containers if c.get("error")]
        yield self.create_text_message(format_results(results, unavailable))
        yield self.create_json_message(
            {
                "query": query,
                "results": results,
                "containers": containers,
                "unavailable_containers": unavailable,
            }
        )
