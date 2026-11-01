"""membase_tools called by the real Agent's function-invocation loop."""

from __future__ import annotations

from agent_framework import Agent
from conftest import ScriptedChatClient, call, say, tool_results

from agent_framework_membase import MembaseContextProvider, bind_user, membase_tools


async def _run(mem, script, *, tools=None, prompt="go", **agent_kwargs):
    chat = ScriptedChatClient([*script, say("done")])
    agent = Agent(
        client=chat, tools=tools if tools is not None else membase_tools(mem.client()), **agent_kwargs
    )
    await agent.run(prompt)
    return chat, tool_results(chat)


def test_tool_set_and_schemas(mem):
    tools = membase_tools(mem.client())
    assert [t.name for t in tools] == ["membase_search", "membase_save", "membase_profile"]
    search = tools[0].parameters()
    assert search["required"] == ["query"]
    assert "ctx" not in search["properties"]  # the framework injects it; the model never sees it
    assert set(tools[1].parameters()["properties"]) == {"content"}
    assert "container" not in search["properties"]
    assert [t.name for t in membase_tools(mem.client(), save=False, profile=False)] == ["membase_search"]


async def test_search_cites_the_memory(mem):
    _, (out,) = await _run(mem, [call("membase_search", {"query": "ledger", "limit": 3})])
    assert "- [Project decisions] We settled on Postgres for the ledger." in out
    assert mem.calls("/v1/search")[0]["json"] == {"q": "ledger", "mode": "full", "limit": 3}


async def test_search_reports_memories_that_could_not_answer(mem):
    mem.results = []
    mem.partial = [
        {"memory_name": "Travel", "reason": "error"},
        {"memory_name": "Reading", "reason": "truncated"},
    ]
    _, (out,) = await _run(mem, [call("membase_search", {"query": "x"})])
    assert "No passages" in out and '"Travel" could not answer' in out
    assert '"Reading" had more passages than the limit let through' in out


async def test_save_lands_in_the_write_target(mem):
    _, (out,) = await _run(mem, [call("membase_save", {"content": "I take the window seat"})])
    (saved,) = mem.calls("/v1/memories/add")
    assert saved["json"] == {"content": "I take the window seat", "title": ""}
    assert saved["params"] == {}  # never confirm=
    assert 'Saved to Membase "Travel"' in out


async def test_save_without_a_write_target_says_how_to_fix_it(mem):
    mem.write_target = None
    _, (out,) = await _run(mem, [call("membase_save", {"content": "x"})])
    assert "did not save" in out and "several Memories and has no write target" in out
    assert "Connect › Developer keys" in out


async def test_save_with_a_read_key_explains_the_refusal(mem):
    mem.status["/v1/memories/add"] = 403
    _, (out,) = await _run(mem, [call("membase_save", {"content": "x"})])
    assert "narrowed or revoked" in out and "Read key cannot write" in out


async def test_profile(mem):
    _, (out,) = await _run(mem, [call("membase_profile", {})])
    assert out == "About the user:\n- Works in Berlin\n- Prefers dark mode"


async def test_profile_not_shared_is_a_clear_message(mem):
    mem.status["/v1/profile-facts"] = 403
    _, (out,) = await _run(mem, [call("membase_profile", {})])
    assert "did not share the profile" in out


async def test_tools_follow_the_sessions_user(mem):
    tools = membase_tools(client_for=lambda u: mem.client(key=f"mbk_{u}"))
    chat = ScriptedChatClient([call("membase_search", {"query": "q"}), say("done")])
    agent = Agent(client=chat, tools=tools)
    session = agent.create_session()
    bind_user(session, "carol")
    await agent.run("go", session=session)
    assert mem.calls("/v1/search")[0]["auth"] == "Bearer mbk_carol"


async def test_tools_refuse_without_a_bound_user(mem):
    tools = membase_tools(client_for=lambda u: mem.client(key=f"mbk_{u}"))
    _, (out,) = await _run(mem, [call("membase_search", {"query": "q"})], tools=tools)
    assert "no Membase user is bound" in out
    assert mem.requests == []


async def test_provider_and_tools_together(mem):
    client = mem.client()
    chat = ScriptedChatClient(
        [
            call("membase_save", {"content": "Ship on Fridays"}),
            say("Saved."),
        ]
    )
    agent = Agent(
        client=chat, tools=membase_tools(client), context_providers=[MembaseContextProvider(client)]
    )
    result = await agent.run("remember: we ship on Fridays")
    assert result.text == "Saved."
    assert "[Project decisions]" in str(chat.seen[0]["options"]["instructions"])
    assert mem.calls("/v1/memories/add")[0]["json"]["content"] == "Ship on Fridays"
