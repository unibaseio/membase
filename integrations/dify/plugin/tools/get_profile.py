"""get_profile: the standing and recent facts Membase keeps about the user."""

from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from membase.errors import MembaseError, PermissionDeniedError

from membase_dify.client import KEYS_PAGE, MembaseToolError, explain, open_client


def _fact(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("content") or item.get("text") or item.get("fact") or item).strip()
    return str(item).strip()


def format_profile(profile: dict) -> str:
    sections = []
    for title, key in (("Standing facts", "static"), ("Recently changed", "dynamic")):
        facts = [f for f in (_fact(i) for i in profile.get(key) or []) if f]
        if facts:
            sections.append(title + ":\n" + "\n".join(f"- {f}" for f in facts))
    results = profile.get("results") or []
    if results:
        sections.append(
            "Relevant to the topic:\n"
            + "\n".join(
                f"- [{r.get('container_name') or 'Membase'}] {str(r.get('content') or '').strip()}"
                if isinstance(r, dict)
                else f"- {r}"
                for r in results
            )
        )
    if profile.get("available") is False:
        sections.insert(
            0, f"The profile could not be read right now: {profile.get('reason') or 'unavailable'}."
        )
    elif profile.get("ready") is False:
        sections.insert(0, "The profile is still loading; what follows may be incomplete.")
    return "\n\n".join(sections) or "The profile is empty."


class GetProfileTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        query = str(tool_parameters.get("query") or "").strip() or None
        try:
            with open_client(self.runtime.credentials) as client:
                profile = client.profile(q=query)
        except PermissionDeniedError as err:
            raise MembaseToolError(
                f"Membase refused to read the profile (403: {err.message}). The key's owner did "
                "not grant the profile to this key, or narrowed or revoked the key. "
                f"Check it under {KEYS_PAGE}."
            ) from err
        except MembaseError as err:
            raise MembaseToolError(explain(err)) from err
        profile = dict(profile or {})
        yield self.create_text_message(format_profile(profile))
        yield self.create_json_message(profile)
