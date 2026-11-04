from __future__ import annotations

import time

import pytest
from conftest import FakeMembase, ScriptedLlm, call, request_text, say, tool_responses
from google.adk.agents import Agent
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import preload_memory
from google.genai import types
from membase import Membase

from membase_google_adk import MembaseMemoryService, MembaseRecallTimeout, membase_tools


async def run(agent: Agent, text: str, memory_service=None, user_id: str = "u1") -> list[Event]:
    sessions = InMemorySessionService()
    await sessions.create_session(app_name="app", user_id=user_id, session_id="s1")
    runner = Runner(app_name="app", agent=agent, session_service=sessions, memory_service=memory_service)
    message = types.Content(role="user", parts=[types.Part(text=text)])
    return [e async for e in runner.run_async(user_id=user_id, session_id="s1", new_message=message)]


def user_event(text: str, author: str = "user") -> Event:
    role = "user" if author == "user" else "model"
    return Event(author=author, content=types.Content(role=role, parts=[types.Part(text=text)]))


# ---------------------------------------------------------------------- memory service


async def test_search_cites_the_memory_and_reports_one_that_could_not_answer(client: Membase, fake: FakeMembase):
    fake.search_body["partial"] = [{"memory_name": "Inbox", "reason": "error"}]
    service = MembaseMemoryService(client=client, max_search_results=3)

    response = await service.search_memory(app_name="app", user_id="u1", query="ledger database")

    texts = [m.content.parts[0].text for m in response.memories]
    assert texts == [
        "[Project decisions] We chose Postgres for the ledger.",
        "[Inbox] This Membase memory could not answer.",
    ]
    assert response.memories[0].id == "mem_eA"
    assert fake.calls("/v1/search") == [{"q": "ledger database", "limit": 3, "mode": "full"}]


async def test_search_is_time_boxed(client: Membase, fake: FakeMembase):
    fake.search_delay = 1.0
    service = MembaseMemoryService(client=client, recall_timeout=0.1)

    started = time.monotonic()
    with pytest.raises(MembaseRecallTimeout):
        await service.search_memory(app_name="app", user_id="u1", query="anything")
    assert time.monotonic() - started < 0.5


async def test_preload_memory_puts_the_passages_in_front_of_the_model(client: Membase):
    model = ScriptedLlm(turns=[say("Postgres.")])
    agent = Agent(name="assistant", model=model, tools=[preload_memory])

    events = await run(agent, "Which database did we pick?", MembaseMemoryService(client=client))

    assert events[-1].content.parts[0].text == "Postgres."
    assert "[Project decisions] We chose Postgres for the ledger." in request_text(model.seen[0])


async def test_preload_memory_goes_on_without_memory_when_recall_times_out(client: Membase, fake: FakeMembase):
    fake.search_delay = 1.0
    model = ScriptedLlm(turns=[say("I don't know yet.")])
    agent = Agent(name="assistant", model=model, tools=[preload_memory])

    events = await run(agent, "Which database?", MembaseMemoryService(client=client, recall_timeout=0.1))

    assert events[-1].content.parts[0].text == "I don't know yet."
    assert "Postgres" not in request_text(model.seen[0])


async def test_sessions_are_not_written_unless_capture_is_on(client: Membase, fake: FakeMembase):
    events = [user_event("I moved to Berlin."), user_event("Noted.", author="assistant")]

    await MembaseMemoryService(client=client).add_events_to_memory(app_name="app", user_id="u1", events=events)
    assert fake.calls("/v1/memories/add") == []

    await MembaseMemoryService(client=client, capture=True).add_events_to_memory(
        app_name="app", user_id="u1", events=events
    )
    assert fake.calls("/v1/memories/add") == [
        {"content": "I moved to Berlin.", "title": "Conversation with an ADK agent"}
    ]


async def test_client_for_picks_each_users_own_memory(client: Membase):
    asked: list[str] = []

    def client_for(user_id: str) -> Membase:
        asked.append(user_id)
        return client

    service = MembaseMemoryService(client_for=client_for)
    await service.search_memory(app_name="app", user_id="alice", query="ledger")
    assert asked == ["alice"]


# ---------------------------------------------------------------------- tools


async def test_the_model_searches_and_reads_cited_passages(client: Membase):
    model = ScriptedLlm(turns=[call("membase_search", query="ledger database"), say("Postgres.")])
    agent = Agent(name="assistant", model=model, tools=membase_tools(client))

    events = await run(agent, "Which database did we pick?")

    assert events[-1].content.parts[0].text == "Postgres."
    [response] = tool_responses(model.seen[1])
    assert "1. [Project decisions] We chose Postgres for the ledger." in response["result"]


async def test_save_and_profile(client: Membase, fake: FakeMembase):
    model = ScriptedLlm(
        turns=[call("membase_save", content="I prefer dark mode."), call("membase_profile"), say("Done.")]
    )
    agent = Agent(name="assistant", model=model, tools=membase_tools(client))

    await run(agent, "Remember that I prefer dark mode.")

    assert fake.calls("/v1/memories/add") == [{"content": "I prefer dark mode.", "title": ""}]
    assert tool_responses(model.seen[1])[-1]["result"] == (
        "Saved to Membase in Project decisions. It shows up in search once the Memory has learned it."
    )
    assert tool_responses(model.seen[2])[-1]["result"] == "About the user:\n- Works in Berlin\n- Started learning Go"


async def test_tool_errors_become_a_sentence_the_model_can_relay(client: Membase, fake: FakeMembase):
    fake.search_status = 403
    fake.memories_status = 422
    model = ScriptedLlm(turns=[call("membase_search", query="x"), call("membase_save", content="y"), say("Sorry.")])
    agent = Agent(name="assistant", model=model, tools=membase_tools(client))

    await run(agent, "hi")

    assert "Membase refused this request (403)" in tool_responses(model.seen[1])[-1]["result"]
    assert "reaches several Memories and has no write target" in tool_responses(model.seen[2])[-1]["result"]


async def test_tools_use_the_user_id_from_the_tool_context(client: Membase):
    asked: list[str] = []

    def client_for(user_id: str) -> Membase:
        asked.append(user_id)
        return client

    model = ScriptedLlm(turns=[call("membase_profile"), say("Hi.")])
    agent = Agent(name="assistant", model=model, tools=membase_tools(client_for=client_for))

    await run(agent, "hi", user_id="bob")
    assert asked == ["bob"]


def test_choosing_tools(client: Membase):
    assert [t.name for t in membase_tools(client, tools=("search", "profile"))] == ["membase_search", "membase_profile"]
    with pytest.raises(ValueError, match="unknown tools"):
        membase_tools(client, tools=("delete",))
