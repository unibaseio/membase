"""Live smoke test against the real Membase API.

Needs MEMBASE_API_KEY (a developer key from Connect > Developer keys in the Membase app).
Without a model key it checks the store and its tools directly; with AWS credentials for
Bedrock (Strands' default model) or OPENAI_API_KEY plus `pip install 'strands-agents[openai]'`,
it also runs one agent turn so injection is exercised end to end.

    MEMBASE_API_KEY=mbk_... python examples/live_smoke.py
    MEMBASE_API_KEY=mbk_... MEMBASE_SMOKE_SAVE=1 python examples/live_smoke.py   # also saves one note

Saving is off unless MEMBASE_SMOKE_SAVE=1, because it writes to a real person's memory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from membase_strands import MembaseMemoryStore


def tool(store: MembaseMemoryStore, name: str):
    return next(t for t in store.get_tools() if t.tool_name == name)


async def main() -> int:
    if not os.environ.get("MEMBASE_API_KEY"):
        print("MEMBASE_API_KEY is not set; nothing to do.")
        return 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    store = MembaseMemoryStore()  # MEMBASE_API_KEY / MEMBASE_BASE_URL from the environment
    containers = store.client.containers.list()["containers"]
    print("Memories in reach:", [c["name"] for c in containers])

    # 1. Explicit search through the tool (SDK timeout, so a cold start is waited out).
    started = time.monotonic()
    out = await tool(store, "membase_search")(query="what do I care about?")
    print(f"\nmembase_search ({time.monotonic() - started:.1f}s):\n{out}")

    # 2. Automatic recall path: time-boxed; now warm, it should answer within budget.
    started = time.monotonic()
    try:
        entries = await store.search("what do I care about?")
        print(f"\nstore.search ({time.monotonic() - started:.1f}s): {len(entries)} entries")
        for e in entries:
            print("  ", e.content[:160])
    except TimeoutError as error:
        print(f"\nstore.search timed out as designed: {error}")

    # 3. Profile (403 if the owner did not share the profile with this key).
    print("\nmembase_profile:\n" + await tool(store, "membase_profile")())

    if os.environ.get("MEMBASE_SMOKE_SAVE") == "1":
        target = containers[0]["name"] if containers else None
        out = await tool(store, "membase_save")(
            content="Membase Strands smoke test: this note can be deleted.", container=target
        )
        print("\nmembase_save:", out)

    # 4. One real agent turn, if a model is configured.
    model = None
    if os.environ.get("OPENAI_API_KEY"):
        from strands.models.openai import OpenAIModel

        model = OpenAIModel(model_id=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    elif os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"):
        model = None  # Strands' default Bedrock model
    else:
        print("\nNo model credentials; skipping the agent turn.")
        return 0

    from strands import Agent
    from strands.memory import MemoryManager

    agent = Agent(
        model=model,
        memory_manager=MemoryManager(stores=[store], search_tool_config=False),
        system_prompt="Answer from the user's memory when it helps, and name the Memory you used.",
    )
    result = await agent.invoke_async("What do you remember about me? One sentence.")
    print("\nAgent:", result)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
