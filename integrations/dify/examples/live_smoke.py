"""Live smoke test: the plugin, loaded by Dify's plugin runtime, against the real Membase API.

    MEMBASE_API_KEY=mbk_... .venv/bin/python examples/live_smoke.py
    MEMBASE_API_KEY=mbk_... MEMBASE_SMOKE_WRITE=1 .venv/bin/python examples/live_smoke.py

Reads only, unless MEMBASE_SMOKE_WRITE=1: then it also saves one clearly labelled note
(to MEMBASE_SMOKE_CONTAINER, a container name or id, when the key reaches several).
MEMBASE_BASE_URL overrides the API root (an https host on membase.ai, membase.com or membase.io).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin"


def load_executor():
    from dify_plugin.config.config import DifyPluginEnv
    from dify_plugin.core.plugin_executor import PluginExecutor
    from dify_plugin.core.plugin_registration import PluginRegistration

    sys.path.insert(0, str(PLUGIN_DIR))
    cwd = os.getcwd()
    os.chdir(PLUGIN_DIR)
    try:
        config = DifyPluginEnv()
        return PluginExecutor(config, PluginRegistration(config))
    finally:
        os.chdir(cwd)


def main() -> int:
    key = os.environ.get("MEMBASE_API_KEY")
    if not key:
        print("MEMBASE_API_KEY is not set; nothing to do.")
        return 0
    from dify_plugin.core.entities.plugin.request import (
        ToolInvokeRequest,
        ToolValidateCredentialsRequest,
    )
    from dify_plugin.core.runtime import Session

    executor = load_executor()
    credentials = {"api_key": key, "base_url": os.environ.get("MEMBASE_BASE_URL", "")}

    def call(tool: str, params: dict) -> None:
        print(f"\n== {tool} {params}")
        request = ToolInvokeRequest(
            user_id="live-smoke",
            provider="membase",
            tool=tool,
            credentials=credentials,
            tool_parameters=params,
        )
        try:
            for message in executor.invoke_tool(Session.empty_session(), request):
                if message.type.value == "text":
                    print(message.message.text)
        except Exception as err:  # noqa: BLE001 - a smoke test prints what the agent would see
            print(f"ERROR {type(err).__name__}: {err}")

    print("== validate credentials (lists containers)")
    executor.validate_tool_provider_credentials(
        Session.empty_session(),
        ToolValidateCredentialsRequest(
            user_id="live-smoke", provider="membase", credentials=credentials
        ),
    )
    print("ok")

    call("get_profile", {})
    call("search_memory", {"query": "What do you know about me?", "limit": 5})
    if os.environ.get("MEMBASE_SMOKE_WRITE") == "1":
        call(
            "save_memory",
            {
                "content": "Membase Dify plugin live smoke test note (safe to delete).",
                "container": os.environ.get("MEMBASE_SMOKE_CONTAINER", ""),
            },
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
