from __future__ import annotations

import time

import pytest
from conftest import FakeMembase, ScriptedChatGenerator, call, tool_results
from haystack import Pipeline
from haystack.components.agents import Agent
from haystack.dataclasses import ChatMessage
from haystack.utils import Secret
from membase import Membase

from membase_haystack import MembaseMemoryRetriever, MembaseMemoryStore, membase_tools


@pytest.fixture
def store(client: Membase) -> MembaseMemoryStore:
    return MembaseMemoryStore(client=client)


# ---------------------------------------------------------------------- store


def test_search_cites_the_memory_and_reports_one_that_could_not_answer(store: MembaseMemoryStore, fake: FakeMembase):
    fake.search_body["partial"] = [{"memory_name": "Inbox", "reason": "error"}]

    memories = store.search_memories(query="ledger database", top_k=3)

    assert [m.text for m in memories] == [
        "[Project decisions] We chose Postgres for the ledger.",
        "[Inbox] This Membase memory could not answer.",
    ]
    assert all(m.is_from("system") for m in memories)
    assert memories[0].meta["id"] == "mem_eA"
    assert fake.calls("/v1/search") == [{"q": "ledger database", "limit": 3, "mode": "full"}]


def test_add_memories_keeps_the_users_own_words(store: MembaseMemoryStore, fake: FakeMembase):
    assert store.add_memories(messages=[ChatMessage.from_assistant("Sure.")]) is None
    assert fake.calls("/v1/memories/add") == []

    store.add_memories(messages=[ChatMessage.from_user("I moved to Berlin."), ChatMessage.from_assistant("Noted.")])
    assert fake.calls("/v1/memories/add") == [{"content": "I moved to Berlin.", "title": ""}]


def test_store_round_trips_without_its_key(monkeypatch: pytest.MonkeyPatch):
    store = MembaseMemoryStore(api_key=Secret.from_env_var("MY_MEMBASE_KEY"), base_url="https://api.test")

    data = store.to_dict()
    assert data["init_parameters"]["api_key"] == {"type": "env_var", "env_vars": ["MY_MEMBASE_KEY"], "strict": True}

    again = MembaseMemoryStore.from_dict(data)
    assert again.base_url == "https://api.test"
    assert again.api_key.to_dict() == store.api_key.to_dict()


# ---------------------------------------------------------------------- retriever


def test_retriever_in_a_pipeline(store: MembaseMemoryStore):
    pipeline = Pipeline()
    pipeline.add_component("memory", MembaseMemoryRetriever(memory_store=store, top_k=2))

    result = pipeline.run({"memory": {"query": "Which database did we pick?"}})

    assert [m.text for m in result["memory"]["memories"]] == ["[Project decisions] We chose Postgres for the ledger."]


def test_retriever_goes_on_without_memory_when_recall_times_out(store: MembaseMemoryStore, fake: FakeMembase):
    fake.search_delay = 1.0
    retriever = MembaseMemoryRetriever(memory_store=store, recall_timeout=0.1)

    started = time.monotonic()
    assert retriever.run(query="anything") == {"memories": []}
    assert time.monotonic() - started < 0.5


def test_retriever_round_trips():
    retriever = MembaseMemoryRetriever(memory_store=MembaseMemoryStore(base_url="https://api.test"), top_k=7)

    again = MembaseMemoryRetriever.from_dict(retriever.to_dict())

    assert again.top_k == 7
    assert again.memory_store.base_url == "https://api.test"


# ---------------------------------------------------------------------- tools


def test_the_agent_searches_and_reads_cited_passages(store: MembaseMemoryStore):
    generator = ScriptedChatGenerator(
        [call("membase_search", query="ledger database"), ChatMessage.from_assistant("Postgres.")]
    )
    agent = Agent(chat_generator=generator, tools=membase_tools(store))

    result = agent.run(messages=[ChatMessage.from_user("Which database did we pick?")])

    assert result["last_message"].text == "Postgres."
    [passages] = tool_results(generator.seen[1])
    assert "1. [Project decisions] We chose Postgres for the ledger." in passages


def test_save_and_profile(store: MembaseMemoryStore, fake: FakeMembase):
    generator = ScriptedChatGenerator(
        [
            call("membase_save", content="I prefer dark mode."),
            call("membase_profile"),
            ChatMessage.from_assistant("Done."),
        ]
    )
    agent = Agent(chat_generator=generator, tools=membase_tools(store))

    agent.run(messages=[ChatMessage.from_user("Remember that I prefer dark mode.")])

    assert fake.calls("/v1/memories/add") == [{"content": "I prefer dark mode.", "title": ""}]
    assert tool_results(generator.seen[1])[-1] == (
        "Saved to Membase in Project decisions. It shows up in search once the Memory has learned it."
    )
    assert tool_results(generator.seen[2])[-1] == "About the user:\n- Works in Berlin\n- Started learning Go"


def test_tool_errors_become_a_sentence_the_model_can_relay(store: MembaseMemoryStore, fake: FakeMembase):
    fake.search_status = 403
    fake.memories_status = 422
    generator = ScriptedChatGenerator(
        [call("membase_search", query="x"), call("membase_save", content="y"), ChatMessage.from_assistant("Sorry.")]
    )
    agent = Agent(chat_generator=generator, tools=membase_tools(store))

    agent.run(messages=[ChatMessage.from_user("hi")])

    assert "Membase refused this request (403)" in tool_results(generator.seen[1])[-1]
    assert "reaches several Memories and has no write target" in tool_results(generator.seen[2])[-1]


def test_choosing_tools(store: MembaseMemoryStore):
    assert [t.name for t in membase_tools(store, tools=("search", "profile"))] == ["membase_search", "membase_profile"]
    with pytest.raises(ValueError, match="unknown tools"):
        membase_tools(store, tools=("delete",))
