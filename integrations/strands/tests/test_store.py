"""The store driven through Strands' own entry points: Agent(memory_manager=MemoryManager(...))."""

from __future__ import annotations

import logging
import time

import pytest
from conftest import ScriptedModel, last_user_text, text, tool_result_text, tool_use
from strands import Agent
from strands.memory import MemoryManager

from membase_strands import MembaseMemoryStore, MembaseRecallTimeout


def make_agent(store: MembaseMemoryStore, model: ScriptedModel, **manager_kwargs) -> Agent:
    manager_kwargs.setdefault("search_tool_config", False)
    return Agent(
        model=model,
        memory_manager=MemoryManager(stores=[store], **manager_kwargs),
        callback_handler=None,
    )


# --------------------------------------------------------------------------- recall


def test_injection_searches_membase_and_cites_the_memory(fake, client):
    store = MembaseMemoryStore(client=client)
    model = ScriptedModel([text("Postgres.")])
    agent = make_agent(store, model)

    result = agent("Which database did we pick for the ledger?")

    assert str(result).strip() == "Postgres."
    assert fake.calls("/v1/search") == [{"q": "Which database did we pick for the ledger?", "mode": "full", "limit": 5}]
    injected = last_user_text(model.seen[0])
    assert "[Project decisions] We chose Postgres for the ledger." in injected
    assert 'source="membase"' in injected
    # Injection is per call: durable history keeps only what the user said.
    assert "Postgres" not in last_user_text(agent.messages[:1])


def test_memory_that_could_not_answer_is_reported_not_silent(fake, client):
    fake.search_body = {
        "items": [],
        "partial": [{"memory_name": "Health", "reason": "error"}, {"memory_name": "Work", "reason": "truncated"}],
    }
    model = ScriptedModel([text("ok")])
    make_agent(MembaseMemoryStore(client=client), model)("anything about my health?")

    injected = last_user_text(model.seen[0])
    assert "[Health] This Membase memory could not answer the search." in injected
    assert "[Work]" not in injected  # truncated: it answered, its passages lost to the limit


def test_cold_start_fails_open_within_budget_and_does_not_stack(fake, client, caplog):
    fake.search_delay = 2.0
    store = MembaseMemoryStore(client=client, recall_timeout=0.3)
    model = ScriptedModel([text("first"), text("second")])
    agent = make_agent(store, model)

    with caplog.at_level(logging.WARNING):
        started = time.monotonic()
        assert str(agent("hello?")).strip() == "first"
        first = time.monotonic() - started
        # The first recall is still waking the memory: the next turn skips at once.
        started = time.monotonic()
        assert str(agent("still there?")).strip() == "second"
        second = time.monotonic() - started

    assert first < 1.5, first
    assert second < 0.3, second
    assert "<memory>" not in last_user_text(model.seen[0])
    assert "<memory>" not in last_user_text(model.seen[1])
    assert len(fake.calls("/v1/search")) == 1
    assert "ran out of time" in caplog.text


async def test_search_raises_recall_timeout_for_the_manager(fake, client):
    fake.search_delay = 1.0
    store = MembaseMemoryStore(client=client, recall_timeout=0.1)
    with pytest.raises(MembaseRecallTimeout):
        await store.search("anything")


def test_revoked_key_during_injection_is_logged_and_the_turn_continues(fake, client, caplog):
    fake.search_status = 403
    model = ScriptedModel([text("answered without memory")])
    with caplog.at_level(logging.WARNING):
        result = make_agent(MembaseMemoryStore(client=client), model)("what do you know?")

    assert str(result).strip() == "answered without memory"
    assert "this Membase key was narrowed or revoked" in caplog.text
    assert "mbk_test" not in caplog.text


def test_no_request_names_a_memory(client):
    with pytest.raises(TypeError):
        MembaseMemoryStore(client=client, container="Project decisions")


# --------------------------------------------------------------------------- tools


def test_membase_search_tool_is_called_by_the_model(fake, client):
    model = ScriptedModel([tool_use("membase_search", {"query": "ledger database"}), text("Postgres.")])
    agent = make_agent(MembaseMemoryStore(client=client), model, injection=False)

    agent("Which database did we pick?")

    assert "membase_search" in model.tool_names[0]
    result = tool_result_text(model.seen[1])
    assert "1. [Project decisions] We chose Postgres for the ledger." in result
    assert fake.calls("/v1/search") == [{"q": "ledger database", "mode": "full"}]


def test_membase_save_tool_writes_to_the_write_target(fake, client):
    model = ScriptedModel([tool_use("membase_save", {"content": "We moved the launch to May."}), text("Saved.")])
    agent = make_agent(MembaseMemoryStore(client=client), model, injection=False)
    agent("Remember: we moved the launch to May.")

    assert fake.calls("/v1/memories/add") == [{"content": "We moved the launch to May.", "title": ""}]
    result = tool_result_text(model.seen[1])
    assert "Saved to Membase in Project decisions" in result and "doc_bm90ZQ" in result
    spec = next(t for t in agent.tool_registry.get_all_tool_specs() if t["name"] == "membase_save")
    assert set(spec["inputSchema"]["json"]["properties"]) == {"content"}


def test_membase_save_without_a_write_target_says_so(fake, client):
    fake.memories_status = 422
    model = ScriptedModel([tool_use("membase_save", {"content": "Keep this."}), text("ok")])
    make_agent(MembaseMemoryStore(client=client), model, injection=False)("Keep this.")

    result = tool_result_text(model.seen[1])
    assert "no write target, so nothing was saved" in result
    assert "Connect › Developer keys" in result


def test_membase_profile_tool(fake, client):
    model = ScriptedModel([tool_use("membase_profile", {}), text("ok")])
    make_agent(MembaseMemoryStore(client=client), model, injection=False)("who am I?")

    result = tool_result_text(model.seen[1])
    assert "Standing facts:\n- Works in Berlin" in result
    assert "Recently changed:\n- Started learning Go" in result


def test_tools_turn_a_403_into_a_clear_sentence(fake, client):
    fake.profile_status = 403
    model = ScriptedModel([tool_use("membase_profile", {}), text("ok")])
    make_agent(MembaseMemoryStore(client=client), model, injection=False)("who am I?")

    assert "this Membase key was narrowed or revoked" in tool_result_text(model.seen[1])
    assert len(fake.calls("/v1/profile")) == 1  # never retried


def test_tool_selection(client):
    store = MembaseMemoryStore(client=client, tools=("search",))
    agent = make_agent(store, ScriptedModel([]))
    assert "membase_search" in agent.tool_names
    assert "membase_save" not in agent.tool_names
    assert "membase_profile" not in agent.tool_names
    with pytest.raises(ValueError):
        MembaseMemoryStore(client=client, tools=("delete",))


# --------------------------------------------------------------------------- writes


def test_capture_is_off_by_default(fake, client):
    store = MembaseMemoryStore(client=client)
    assert store.writable is False
    with pytest.raises(ValueError, match="no writable stores"):
        MemoryManager(stores=[store], add_tool_config=True)
    with pytest.raises(ValueError, match="needs capture=True"):
        MembaseMemoryStore(client=client, extraction=True)

    model = ScriptedModel([text("Sure.")])
    make_agent(store, model)("My cat is called Miso.")
    assert fake.calls("/v1/memories/add") == []


async def test_direct_write_without_capture_is_refused(client):
    store = MembaseMemoryStore(client=client)
    with pytest.raises(ValueError, match="capture is off"):
        await store.add("x")
    with pytest.raises(ValueError, match="capture is off"):
        await store.add_messages([{"role": "user", "content": [{"text": "x"}]}])


def test_capture_with_extraction_saves_the_users_words(fake, client):
    store = MembaseMemoryStore(client=client, capture=True, extraction=True)
    model = ScriptedModel([text("Got it, Postgres it is.")])
    make_agent(store, model, injection=False)("We decided on Postgres for the ledger.")

    # The sync Agent call flushes extraction at the end of the invocation.
    assert fake.calls("/v1/memories/add") == [
        {"content": "We decided on Postgres for the ledger.", "title": "Conversation with a Strands agent"}
    ]


def test_capture_enables_the_managers_add_memory_tool(fake, client):
    store = MembaseMemoryStore(client=client, capture=True)
    model = ScriptedModel([tool_use("add_memory", {"entries": ["Launch moved to May."]}), text("Saved.")])
    make_agent(store, model, injection=False, add_tool_config=True)("Remember the launch moved to May.")

    assert fake.calls("/v1/memories/add") == [{"content": "Launch moved to May.", "title": ""}]


async def test_captured_write_with_revoked_key_raises_for_the_manager(fake, client):
    fake.memories_status = 403
    store = MembaseMemoryStore(client=client, capture=True)
    with pytest.raises(PermissionError, match="narrowed or revoked"):
        await store.add("x")


async def test_captured_write_without_a_write_target_raises_for_the_manager(fake, client):
    from membase.errors import WriteTargetRequiredError

    fake.memories_status = 422
    store = MembaseMemoryStore(client=client, capture=True)
    with pytest.raises(WriteTargetRequiredError):
        await store.add("x")


def test_api_host_defaults_to_membase_com_whatever_the_sdk_defaults_to(monkeypatch):
    monkeypatch.delenv("MEMBASE_BASE_URL", raising=False)
    monkeypatch.delenv("MEMBASE_LOCAL", raising=False)
    assert MembaseMemoryStore(api_key="mbk_x").client.base_url == "https://api.app.membase.com"
    monkeypatch.setenv("MEMBASE_BASE_URL", "https://env.membase.test")
    assert MembaseMemoryStore(api_key="mbk_x").client.base_url == "https://env.membase.test"
    store = MembaseMemoryStore(api_key="mbk_x", base_url="https://arg.membase.test")
    assert store.client.base_url == "https://arg.membase.test"
