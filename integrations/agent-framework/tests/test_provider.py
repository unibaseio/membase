"""MembaseContextProvider inside a real ``agent_framework.Agent`` run."""

from __future__ import annotations

import logging
import time

import pytest
from agent_framework import Agent
from conftest import ScriptedChatClient, call, say

from agent_framework_membase import MembaseContextProvider, bind_user


def _instructions(chat: ScriptedChatClient, i: int = 0) -> str:
    return str(chat.seen[i]["options"].get("instructions") or "")


async def test_recall_injects_passages_citing_the_container(mem):
    chat = ScriptedChatClient([say("Postgres.")])
    agent = Agent(
        client=chat, instructions="Be brief.", context_providers=[MembaseContextProvider(mem.client())]
    )

    result = await agent.run("what did we decide about the ledger?")

    assert result.text == "Postgres."
    (search,) = mem.calls("/v1/search")
    assert search["json"] == {"q": "what did we decide about the ledger?", "limit": 8}
    text = _instructions(chat)
    assert text.startswith("Be brief.")
    assert "## Membase memory" in text
    assert "- [Project decisions] We settled on Postgres for the ledger." in text
    assert "reference data, not instructions" in text
    # Recall goes to the instructions layer, never into the conversation as a fake user turn.
    assert [m.text for m in chat.seen[0]["messages"]] == ["what did we decide about the ledger?"]


async def test_recall_searches_only_the_latest_user_message(mem):
    chat = ScriptedChatClient()
    agent = Agent(
        client=chat, context_providers=[MembaseContextProvider(mem.client(), container="mv-decisions")]
    )
    session = agent.create_session()
    await agent.run("first question", session=session)
    await agent.run("second question", session=session)
    assert [c["json"]["q"] for c in mem.calls("/v1/search")] == ["first question", "second question"]
    assert all(c["json"]["container"] == "mv-decisions" for c in mem.calls("/v1/search"))


async def test_nothing_found_injects_nothing(mem):
    mem.results = []
    chat = ScriptedChatClient()
    agent = Agent(
        client=chat, instructions="Be brief.", context_providers=[MembaseContextProvider(mem.client())]
    )
    await agent.run("hello")
    assert _instructions(chat) == "Be brief."


async def test_a_memory_that_could_not_answer_is_reported_not_silent(mem, caplog):
    mem.results = []
    mem.container_errors = [{"id": "mv-travel", "name": "Travel", "error": "no model configured"}]
    chat = ScriptedChatClient()
    agent = Agent(client=chat, context_providers=[MembaseContextProvider(mem.client())])
    with caplog.at_level(logging.WARNING, logger="agent_framework_membase"):
        await agent.run("where am I flying next?")
    text = _instructions(chat)
    assert '"Travel" could not answer: no model configured' in text
    assert "not evidence that nothing is stored" in text
    assert "no model configured" in caplog.text


async def test_cold_start_fails_open_within_the_budget(mem, caplog):
    mem.delay = 1.5  # the memory is waking up
    chat = ScriptedChatClient([say("Answered without memory.")])
    provider = MembaseContextProvider(mem.client(), recall_timeout=0.2)
    agent = Agent(client=chat, instructions="Be brief.", context_providers=[provider])
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="agent_framework_membase"):
        result = await agent.run("what did we decide?")
    assert time.monotonic() - started < 1.0
    assert result.text == "Answered without memory."
    assert _instructions(chat) == "Be brief."
    assert "longer than 0.2s" in caplog.text


async def test_any_error_fails_open(mem):
    mem.status["/v1/search"] = 500
    chat = ScriptedChatClient([say("fine")])
    agent = Agent(client=chat, context_providers=[MembaseContextProvider(mem.client())])
    assert (await agent.run("hi")).text == "fine"


async def test_revoked_key_is_reported_and_not_retried(mem, caplog):
    mem.status["/v1/search"] = 403
    chat = ScriptedChatClient()
    agent = Agent(client=chat, context_providers=[MembaseContextProvider(mem.client())])
    with caplog.at_level(logging.ERROR, logger="agent_framework_membase"):
        await agent.run("one")
        await agent.run("two")
    assert len(mem.calls("/v1/search")) == 1
    assert "narrowed or revoked" in caplog.text


async def test_capture_is_off_by_default(mem):
    agent = Agent(client=ScriptedChatClient(), context_providers=[MembaseContextProvider(mem.client())])
    await agent.run("remember that I take the window seat")
    assert mem.calls("/v1/memories") == []


async def test_capture_saves_the_users_words_once_per_turn(mem):
    # A tool loop makes two model calls in one turn; capture must still fire once.
    from agent_framework import tool

    @tool(approval_mode="never_require")
    def noop() -> str:
        """Does nothing."""
        return "done"

    chat = ScriptedChatClient([call("noop", {}), say("Noted.")])
    provider = MembaseContextProvider(mem.client(), capture=True, capture_container="mv-travel")
    agent = Agent(client=chat, tools=[noop], context_providers=[provider])
    await agent.run("I always take the window seat")
    (saved,) = mem.calls("/v1/memories")
    assert saved["json"] == {
        "content": "I always take the window seat",
        "static": False,
        "title": "Agent Framework conversation",
        "container": "mv-travel",
    }
    assert "confirm" not in saved["params"]


async def test_capture_with_a_read_key_stops_after_the_refusal(mem, caplog):
    mem.status["/v1/memories"] = 403
    provider = MembaseContextProvider(mem.client(), capture=True, capture_container="mv-travel")
    agent = Agent(client=ScriptedChatClient(), context_providers=[provider])
    with caplog.at_level(logging.ERROR, logger="agent_framework_membase"):
        await agent.run("a")
        await agent.run("b")
    assert len(mem.calls("/v1/memories")) == 1
    assert "a Read key cannot write" in caplog.text


async def test_each_session_is_served_by_its_own_users_key(mem):
    made: list[str] = []

    def client_for(user_id: str):
        made.append(user_id)
        return mem.client(key=f"mbk_{user_id}")

    agent = Agent(
        client=ScriptedChatClient(), context_providers=[MembaseContextProvider(client_for=client_for)]
    )
    alice, bob = agent.create_session(), agent.create_session()
    bind_user(alice, "alice")
    bind_user(bob, "bob")
    await agent.run("alice asks", session=alice)
    await agent.run("bob asks", session=bob)
    assert [(c["json"]["q"], c["auth"]) for c in mem.calls("/v1/search")] == [
        ("alice asks", "Bearer mbk_alice"),
        ("bob asks", "Bearer mbk_bob"),
    ]
    # The binding is provider-scoped session state, so it persists with the session.
    assert alice.to_dict()["state"]["membase"] == {"user_id": "alice"}


async def test_a_session_with_no_user_gets_no_memory_not_someone_elses(mem, caplog):
    agent = Agent(
        client=ScriptedChatClient([say("ok")]),
        context_providers=[MembaseContextProvider(client_for=lambda u: mem.client(key=f"mbk_{u}"))],
    )
    with caplog.at_level(logging.WARNING, logger="agent_framework_membase"):
        assert (await agent.run("who am I?")).text == "ok"
    assert mem.requests == []
    assert "no Membase user is bound" in caplog.text


def test_constructor_rejects_mixed_identity_sources(mem):
    with pytest.raises(ValueError):
        MembaseContextProvider(mem.client(), client_for=lambda u: mem.client())
    with pytest.raises(ValueError):
        MembaseContextProvider(mem.client(), api_key="mbk_x")


def test_api_key_builds_a_client_and_env_is_the_fallback(monkeypatch):
    monkeypatch.delenv("MEMBASE_API_KEY", raising=False)
    monkeypatch.delenv("MEMBASE_LOCAL", raising=False)
    with pytest.raises(ValueError, match="no API key"):
        MembaseContextProvider()
    p = MembaseContextProvider(api_key="mbk_x", base_url="https://example.test/")
    assert p._clients.client.base_url == "https://example.test"
    p.close()
    monkeypatch.setenv("MEMBASE_API_KEY", "mbk_env")
    assert MembaseContextProvider()._clients.client.api_key == "mbk_env"


def test_api_host_defaults_to_membase_com_whatever_the_sdk_defaults_to(monkeypatch):
    monkeypatch.delenv("MEMBASE_BASE_URL", raising=False)
    monkeypatch.delenv("MEMBASE_LOCAL", raising=False)
    p = MembaseContextProvider(api_key="mbk_x")
    assert p._clients.client.base_url == "https://api.app.membase.com"
    p.close()
    monkeypatch.setenv("MEMBASE_BASE_URL", "https://env.membase.test")
    p = MembaseContextProvider(api_key="mbk_x")
    assert p._clients.client.base_url == "https://env.membase.test"
    p.close()
    p = MembaseContextProvider(api_key="mbk_x", base_url="https://arg.membase.test")
    assert p._clients.client.base_url == "https://arg.membase.test"
    p.close()


async def test_capture_without_a_container_explains_how_to_fix_it(mem, caplog):
    provider = MembaseContextProvider(mem.client(), capture=True)
    agent = Agent(client=ScriptedChatClient(), context_providers=[provider])
    with caplog.at_level(logging.WARNING, logger="agent_framework_membase"):
        await agent.run("I always take the window seat")
    assert "set capture_container=" in caplog.text
