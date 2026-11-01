"""get_profile: the profile facts Membase keeps about the user, newest first."""

from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from membase.errors import MembaseError, PermissionDeniedError

from membase_dify.client import KEYS_PAGE, MembaseToolError, explain, open_client


def format_profile(profile: dict) -> str:
    items = [f for f in profile.get("items") or [] if isinstance(f, dict)]
    facts = [str(f.get("content") or "").strip() for f in items]
    facts = [f for f in facts if f]
    if not facts:
        return "The profile is empty."
    return "About the user:\n" + "\n".join(f"- {f}" for f in facts)


class GetProfileTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        try:
            with open_client(self.runtime.credentials) as client:
                profile = client.profile_facts.list()
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
