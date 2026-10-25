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
    toolkit = MembaseToolkit(client=fake.client(), container="mv-reading", k=3)
    assert [t.name for t in toolkit.get_tools()] == [t.name for t in tools]
    assert toolkit.get_tools()[0].k == 3 and toolkit.get_tools()[1].container == "mv-reading"


def test_search_cites_container_and_keeps_raw_artifact(fake: FakeMembase) -> None:
    msg = call(MembaseSearchTool(client=fake.client(), k=2), {"query": "ledger"})
    assert msg.content.splitlines()[0].startswith("- [Project decisions] Passage 1")
    assert "[Reading notes]" in msg.content
    assert msg.artifact["results"][0]["container"] == "mv-decisions"


def test_search_by_memory_name(fake: FakeMembase) -> None:
    call(MembaseSearchTool(client=fake.client()), {"query": "q", "container": "reading NOTES"})
    assert fake.paths() == ["/v1/containers", "/v1/search"]
    assert fake.requests[-1]["body"]["container"] == "mv-reading"


def test_search_unknown_memory_lists_choices(fake: FakeMembase) -> None:
    msg = call(MembaseSearchTool(client=fake.client()), {"query": "q", "container": "Diary"})
    assert "No single Memory called 'Diary'" in msg.content
    assert "'Project decisions', 'Reading notes'" in msg.content
    assert "/v1/search" not in fake.paths()


def test_search_reports_unavailable_memories(fake: FakeMembase) -> None:
    fake.container_errors = {"mv-decisions": "no model", "mv-reading": "dormant"}
    msg = call(MembaseSearchTool(client=fake.client()), {"query": "q"})
    assert msg.content.startswith("No passages came back.")
    assert "Project decisions: no model" in msg.content


def test_search_revoked_key_message(fake: FakeMembase) -> None:
    fake.revoked_keys.add("mbk_alice")
    msg = call(MembaseSearchTool(client=fake.client()), {"query": "q"})
    assert msg.status == "error"
    assert "narrowed or revoked" in msg.content and "do not retry" in msg.content


def test_save_static_fact_goes_to_profile(fake: FakeMembase) -> None:
    msg = call(
        MembaseSaveTool(client=fake.client()), {"content": "I prefer dark mode.", "static": True}
    )
    assert msg.content == "Recorded in the user's profile."
    assert fake.requests[-1]["body"] == {
        "content": "I prefer dark mode.",
        "static": True,
        "title": "",
    }


def test_save_note_to_named_memory(fake: FakeMembase) -> None:
    msg = call(
        MembaseSaveTool(client=fake.client()),
        {"content": "We settled on Postgres.", "container": "Project decisions"},
    )
    assert msg.content.startswith("Saved to Project decisions.")
    assert fake.requests[-1]["body"]["container"] == "mv-decisions"
    assert "confirm" not in fake.requests[-1]["params"]


def test_save_default_container(fake: FakeMembase) -> None:
    call(MembaseSaveTool(client=fake.client(), container="mv-reading"), {"content": "x"})
    assert fake.paths() == ["/v1/memories"]
    assert fake.requests[-1]["body"]["container"] == "mv-reading"


def test_save_without_container_asks_which(fake: FakeMembase) -> None:
    msg = call(MembaseSaveTool(client=fake.client()), {"content": "x"})
    assert "call again with container set to one of: 'Project decisions', 'Reading notes'" in (
        msg.content
    )


def test_save_with_a_read_key_is_refused_clearly(fake: FakeMembase) -> None:
    fake.read_only_keys.add("mbk_alice")
    msg = call(MembaseSaveTool(client=fake.client()), {"content": "x", "static": True})
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
        call(tools[1], {"content": "x", "static": True}, {"configurable": {"user_id": uid}})
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
