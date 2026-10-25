"""Live smoke test against the real Membase API.

    MEMBASE_API_KEY=mbk_... python examples/live_smoke.py

Reads only, unless MEMBASE_SMOKE_WRITE=1 (then it saves one clearly labelled note through the
save tool; that needs a Read & write key, and a container id in MEMBASE_SMOKE_CONTAINER when
the key reaches several Memories). Without MEMBASE_API_KEY it says so and exits 0.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone

from membase import Membase
from membase.errors import PermissionDeniedError

from langchain_membase import MembaseRetriever, membase_tools
from langchain_membase._common import DEFAULT_BASE_URL

QUERY = os.environ.get("MEMBASE_SMOKE_QUERY", "what do you know about me")


def main() -> int:
    if not os.environ.get("MEMBASE_API_KEY"):
        print("MEMBASE_API_KEY is not set; skipping the live smoke test.")
        return 0
    logging.basicConfig(level=logging.WARNING)
    # MEMBASE_API_KEY from the environment; the package's default host unless MEMBASE_BASE_URL.
    client = Membase(base_url=os.environ.get("MEMBASE_BASE_URL") or DEFAULT_BASE_URL)
    print("base url:", client.base_url)

    containers = client.containers.list()["containers"]
    print(f"containers in reach: {[c['name'] for c in containers]}")

    t = time.monotonic()
    docs = MembaseRetriever(client=client, k=5).invoke(QUERY)
    print(f"retriever: {len(docs)} documents in {time.monotonic() - t:.1f}s")
    for d in docs:
        print(f"  [{d.metadata['container_name']}] {d.page_content[:100]!r}")

    search, save, profile = membase_tools(client)
    msg = search.invoke(
        {"type": "tool_call", "name": search.name, "args": {"query": QUERY}, "id": "smoke-1"}
    )
    print("search tool:\n" + msg.content[:600])

    msg = profile.invoke({"type": "tool_call", "name": profile.name, "args": {}, "id": "smoke-2"})
    print("profile tool:\n" + msg.content[:600])

    try:
        from langchain.agents import create_agent  # noqa: F401

        _middleware_smoke(client)
    except ImportError:
        print("middleware: skipped (pip install 'langchain-membase[agents]')")

    if os.environ.get("MEMBASE_SMOKE_WRITE") == "1":
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        args = {"content": f"langchain-membase live smoke note, {stamp}."}
        if os.environ.get("MEMBASE_SMOKE_CONTAINER"):
            args["container"] = os.environ["MEMBASE_SMOKE_CONTAINER"]
        msg = save.invoke({"type": "tool_call", "name": save.name, "args": args, "id": "smoke-3"})
        print("save tool:", msg.content)
    else:
        print("save tool: skipped (set MEMBASE_SMOKE_WRITE=1 to write one note)")
    return 0


def _middleware_smoke(client: Membase) -> None:
    from langchain.agents import create_agent
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage, SystemMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from langchain_membase import MembaseRecallMiddleware

    seen: list[str] = []

    class Echo(BaseChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append("\n".join(m.text for m in messages if isinstance(m, SystemMessage)))
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

        @property
        def _llm_type(self) -> str:
            return "echo"

    timeout = float(os.environ.get("MEMBASE_SMOKE_RECALL_TIMEOUT", "8"))
    agent = create_agent(Echo(), middleware=[MembaseRecallMiddleware(client, timeout=timeout)])
    t = time.monotonic()
    agent.invoke({"messages": [{"role": "user", "content": QUERY}]})
    injected = "From the user's memory" in seen[0]
    print(
        f"middleware: recall injected={injected} in {time.monotonic() - t:.1f}s "
        f"(budget {timeout:g}s; a cold memory may need a second run)"
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PermissionDeniedError as e:
        print(f"403: this Membase key was narrowed or revoked: {e}")
        sys.exit(1)
