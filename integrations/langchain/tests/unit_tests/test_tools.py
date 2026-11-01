from __future__ import annotations

import pytest
from langchain_core.messages import ToolMessage

from langchain_membase import (
    MembaseProfileTool,
    MembaseSaveTool,
    MembaseSearchTool,
    MembaseToolkit,
    MissingUserIdError,
    membase_tools,
)

from .conftest import FakeMembase


def call(tool, args: dict, config: dict | None = None) -> ToolMessage:
    msg = tool.invoke({"type": "tool_call", "name": tool.name, "args": args, "id": "c1"}, config)
    assert isinstance(msg, ToolMessage)
    return msg


def test_membase_tools_has_no_destructive_tool(fake: FakeMembase) -> None:
    tools = membase_tools(fake.client())
    assert [t.name for t in tools] == ["membase_search", "membase_save", "membase_profile"]
    assert [t.name for t in membase_tools(fake.client(), save=False, profile=False)] == [
        "membase_search"
    ]
    assert all(t.client is tools[0].client for t in tools)
    toolkit = MembaseToolkit(client=fake.client(), k=3)
    assert [t.name for t in toolkit.get_tools()] == [t.name for t in tools]
    assert toolkit.get_tools()[0].k == 3


def test_no_tool_takes_a_container(fake: FakeMembase) -> None:
    for tool in membase_tools(fake.client()):
        assert "container" not in tool.args and "static" not in tool.args


def test_search_cites_memory_and_keeps_raw_artifact(fake: FakeMembase) -> None:
    msg = call(MembaseSearchTool(client=fake.client(), k=2), {"query": "ledger"})
    assert msg.content.splitlines()[0].startswith("- [Project decisions] Passage 1")
    assert "[Reading notes]" in msg.content
    assert msg.artifact["items"][0]["memory_name"] == "Project decisions"
    assert fake.requests[-1]["body"] == {"q": "ledger", "mode": "full", "limit": 2}


def test_search_reports_unavailable_memories(fake: FakeMembase) -> None:
    fake.memory_errors = {"Project decisions", "Reading notes"}
    msg = call(MembaseSearchTool(client=fake.client()), {"query": "q"})
    assert msg.content.startswith("No passages came back.")
    assert "could not answer" in msg.content and "Project decisions; Reading notes" in msg.content


def test_search_names_truncated_memories() -> None:
    from langchain_membase._common import format_search

    text = format_search(
        {
            "items": [{"type": "memory", "content": "Postgres", "memory_name": "Work"}],
            "partial": [{"memory_name": "Life", "reason": "truncated"}],
        }
    )
    assert text.startswith("- [Work] Postgres") and "did not fit the limit: Life" in text


def test_search_revoked_key_message(fake: FakeMembase) -> None:
    fake.revoked_keys.add("mbk_alice")
    msg = call(MembaseSearchTool(client=fake.client()), {"query": "q"})
    assert msg.status == "error"
    assert "narrowed or revoked" in msg.content and "do not retry" in msg.content


def test_save_lands_in_the_write_target(fake: FakeMembase) -> None:
    msg = call(MembaseSaveTool(client=fake.client()), {"content": "We settled on Postgres."})
    assert msg.content.startswith("Saved to Project decisions.")
    assert fake.paths() == ["/v1/memories"]
    assert fake.requests[-1]["body"] == {"content": "We settled on Postgres.", "title": ""}
    assert "confirm" not in fake.requests[-1]["params"]


def test_save_without_a_write_target_says_so(fake: FakeMembase) -> None:
    fake.write_target = None
    msg = call(MembaseSaveTool(client=fake.client()), {"content": "x"})
    assert msg.status == "error"
    assert "several Memories and has no write target, so nothing was saved" in msg.content
    assert "Connect › Developer keys" in msg.content


def test_save_with_a_read_key_is_refused_clearly(fake: FakeMembase) -> None:
    fake.read_only_keys.add("mbk_alice")
    msg = call(MembaseSaveTool(client=fake.client()), {"content": "x"})
    assert msg.status == "error" and "narrowed or revoked" in msg.content


def test_profile(fake: FakeMembase) -> None:
    msg = call(MembaseProfileTool(client=fake.client()), {"topic": "books"})
    assert "Standing facts:\n- The user behind mbk_alice prefers dark mode." in msg.content
    assert "Recently changed:\n- Started reading" in msg.content
    assert "Relevant to the topic:\n- [Project decisions]" in msg.content
    assert fake.requests[-1]["params"] == {"q": "books"}


def test_profile_not_granted(fake: FakeMembase) -> None:
    fake.revoked_keys.add("mbk_alice")
    msg = call(MembaseProfileTool(client=fake.client()), {})
    assert "only if its owner ticked it" in msg.content


def test_tools_route_each_user_to_their_key(fake: FakeMembase) -> None:
    tools = membase_tools(client_for=lambda uid: fake.client(f"mbk_{uid}"))
    for uid in ("alice", "bob"):
        call(tools[0], {"query": "q"}, {"configurable": {"user_id": uid}})
        call(tools[1], {"content": "x"}, {"configurable": {"user_id": uid}})
    assert [r["key"] for r in fake.requests] == ["mbk_alice"] * 2 + ["mbk_bob"] * 2


def test_tools_without_user_id_raise(fake: FakeMembase) -> None:
    tool = MembaseSearchTool(client_for=lambda uid: fake.client(f"mbk_{uid}"))
    with pytest.raises(MissingUserIdError):
        call(tool, {"query": "q"})
    assert fake.requests == []


async def test_async_tool_call_with_user(fake: FakeMembase) -> None:
    tool = MembaseSearchTool(client_for=lambda uid: fake.client(f"mbk_{uid}"), k=1)
    msg = await tool.ainvoke(
        {"type": "tool_call", "name": tool.name, "args": {"query": "q"}, "id": "c1"},
        {"configurable": {"user_id": "erin"}},
    )
    assert "mbk_erin" in msg.content
