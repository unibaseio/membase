"""Live smoke test against the real Membase API (no LLM key needed).

    MEMBASE_API_KEY=mbk_... python examples/live_smoke.py ["a question for your memory"]

A real ``agent_framework.Agent`` runs with the Membase context provider and tools attached;
its chat client is a stand-in that calls the tools the way a model would and prints what the
model would have seen. Writes nothing unless MEMBASE_SMOKE_WRITE=1 (then it saves one note to
MEMBASE_SMOKE_CONTAINER, a container id or name). Exits 0 without a key.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import MutableSequence
from typing import Any

from agent_framework import Agent, BaseChatClient, ChatResponse, Content, FunctionInvocationLayer, Message
from membase import Membase

from agent_framework_membase import MembaseContextProvider, membase_tools
from agent_framework_membase._common import DEFAULT_BASE_URL


class StandInModel(FunctionInvocationLayer[Any], BaseChatClient[Any]):
    """Calls each scripted tool once, then reports what it was given."""

    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        super().__init__()
        self.calls = list(calls)
        self.instructions = ""

    def _inner_get_response(  # type: ignore[override]
        self, *, messages: MutableSequence[Message], stream: bool, options: dict[str, Any], **kwargs: Any
    ) -> Any:
        async def _get() -> ChatResponse:
            self.instructions = self.instructions or str(options.get("instructions") or "")
            if self.calls:
                name, args = self.calls.pop(0)
                fc = Content.from_function_call(call_id=f"call-{name}", name=name, arguments=args)
                return ChatResponse(messages=Message(role="assistant", contents=[fc]))
            results = [c.result for m in messages for c in m.contents if c.type == "function_result"]
            return ChatResponse(messages=Message(role="assistant", contents=[str(results)]))

        return _get()


async def main() -> int:
    if not os.environ.get("MEMBASE_API_KEY"):
        print("MEMBASE_API_KEY is not set; skipping the live smoke test.")
        return 0
    logging.basicConfig(level=logging.WARNING)
    question = sys.argv[1] if len(sys.argv) > 1 else "What do you know about me?"
    # MEMBASE_API_KEY from the environment; the host is the package's default unless MEMBASE_BASE_URL is set.
    client = Membase(base_url=os.environ.get("MEMBASE_BASE_URL") or DEFAULT_BASE_URL)

    print("containers:", [c.get("name") for c in client.containers.list().get("containers", [])])

    calls: list[tuple[str, dict[str, Any]]] = [("membase_search", {"query": question, "limit": 5})]
    if os.environ.get("MEMBASE_SMOKE_PROFILE") == "1":
        calls.append(("membase_profile", {}))
    if os.environ.get("MEMBASE_SMOKE_WRITE") == "1":
        calls.append(
            (
                "membase_save",
                {
                    "content": f"Smoke test note from agent-framework-membase at {time.ctime()}.",
                    "container": os.environ.get("MEMBASE_SMOKE_CONTAINER"),
                },
            )
        )
    model = StandInModel(calls)
    # A generous budget here: the first search after a quiet spell wakes the memory.
    provider = MembaseContextProvider(
        client, recall_timeout=float(os.environ.get("MEMBASE_RECALL_TIMEOUT", "60"))
    )
    agent = Agent(client=model, tools=membase_tools(client), context_providers=[provider])

    started = time.monotonic()
    response = await agent.run(question)
    print(f"\n-- run took {time.monotonic() - started:.1f}s")
    print("\n-- instructions the model saw (recall):\n" + (model.instructions or "(none: nothing recalled)"))
    print("\n-- tool results:\n" + response.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
