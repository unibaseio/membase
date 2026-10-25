"""Live smoke test: the provider, loaded by Hermes, against the real Membase API.

    MEMBASE_API_KEY=mbk_... .venv/bin/python examples/live_smoke.py           # read-only
    MEMBASE_API_KEY=mbk_... .venv/bin/python examples/live_smoke.py --write   # also saves one note

Hermes must be importable (see README, "Development"). The script runs the plugin's register(ctx)
in a throwaway HERMES_HOME and drives the provider through Hermes' MemoryManager exactly as an agent
turn does: prefetch (time-boxed recall), then the tools. Without --write nothing is written to the
account. Optional: MEMBASE_BASE_URL, MEMBASE_CONTAINER. (The offline tests cover Hermes' plugin
loader itself.)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--query", default="What do you know about me and my current projects?")
    parser.add_argument("--write", action="store_true", help="also save one note with membase_save")
    args = parser.parse_args()

    if not os.environ.get("MEMBASE_API_KEY"):
        print("MEMBASE_API_KEY is not set; nothing to check. Mint a key under Connect > Developer keys.")
        return 0

    home = Path(tempfile.mkdtemp(prefix="hermes-membase-smoke-"))
    try:
        os.environ["HERMES_HOME"] = str(home)
        from agent.memory_manager import MemoryManager

        spec = importlib.util.spec_from_file_location("hermes_membase_plugin", PLUGIN_ROOT / "__init__.py",
                                                      submodule_search_locations=[str(PLUGIN_ROOT)])
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)

        class Ctx:  # what register(ctx) needs from Hermes' plugin context
            provider = None

            def register_memory_provider(self, provider):
                self.provider = provider

        ctx = Ctx()
        plugin.register(ctx)
        provider = ctx.provider
        if provider is None or not provider.is_available():
            print("The unibaseio-membase provider is not available:", provider and provider.unavailable_reason())
            return 1
        mgr = MemoryManager()
        mgr.add_provider(provider)
        mgr.initialize_all(session_id="live-smoke", platform="cli", hermes_home=str(home), agent_context="primary")
        print("settings:", json.dumps(provider.get_status_config({}), indent=2))

        started = time.monotonic()
        block = mgr.prefetch_all(args.query, session_id="live-smoke")
        print(f"\nprefetch ({time.monotonic() - started:.1f} s, budget 8 s, fails open):")
        print(block or "(nothing injected: no match, or the memory is still waking; run again in a minute)")
        print(mgr.describe_recall())

        def call(name: str, **tool_args: object) -> dict:
            started = time.monotonic()
            out = json.loads(mgr.handle_tool_call(name, tool_args))
            print(f"\n{name} ({time.monotonic() - started:.1f} s):\n{json.dumps(out, indent=2, ensure_ascii=False)[:3000]}")
            return out

        search = call("membase_search", query=args.query, limit=5)
        call("membase_profile")
        if args.write:
            call("membase_save", content="Live smoke test from the Hermes Membase provider; safe to delete.")
        mgr.shutdown_all()
        return 1 if "error" in search else 0
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
