"""Through LangChain v1 `create_agent` (a LangGraph graph) with a scripted chat model."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from langchain_membase import MembaseRecallMiddleware, membase_tools

from .conftest import FakeMembase


class ScriptedModel(BaseChatModel):
    """Answers with the scripted messages in order and records what it was sent."""

    replies: list[AIMessage]
    seen: list[list[BaseMessage]] = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.seen.append(list(messages))
        reply = self.replies[min(len(self.seen), len(self.replies)) - 1]
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools, **kwargs: Any) -> ScriptedModel:
        return self

    @property
    def _llm_type(self) -> str:
        return "scripted"


def search_then_answer() -> ScriptedModel:
    return ScriptedModel(
        replies=[
            AIMessage(
                content="",
                tool_calls=[{"name": "membase_search", "args": {"query": "ledger"}, "id": "t1"}],
            ),
            AIMessage(content="You chose Postgres (Project decisions)."),
        ]
    )


def system_text(messages: list[BaseMessage]) -> str:
    return "\n".join(m.text for m in messages if isinstance(m, SystemMessage))


def test_agent_calls_the_search_tool_per_user(fake: FakeMembase) -> None:
    model = search_then_answer()
    agent = create_agent(model, tools=membase_tools(client_for=lambda u: fake.client(f"mbk_{u}")))
    out = agent.invoke(
        {"messages": [{"role": "user", "content": "What did we pick for the ledger?"}]},
        config={"configurable": {"user_id": "alice"}},
    )
    tool_msg = next(m for m in out["messages"] if isinstance(m, ToolMessage))
    assert "[Project decisions]" in tool_msg.content and "mbk_alice" in tool_msg.content
    assert out["messages"][-1].content == "You chose Postgres (Project decisions)."
    assert [r["key"] for r in fake.requests] == ["mbk_alice"]


@dataclass
class Ctx:
    user_id: str


def test_user_id_from_langgraph_context(fake: FakeMembase) -> None:
    agent = create_agent(
        search_then_answer(),
        tools=membase_tools(client_for=lambda u: fake.client(f"mbk_{u}")),
        middleware=[MembaseRecallMiddleware(client_for=lambda u: fake.client(f"mbk_{u}"))],
        context_schema=Ctx,
    )
    agent.invoke({"messages": [{"role": "user", "content": "ledger?"}]}, context=Ctx("bob"))
    assert {r["key"] for r in fake.requests} == {"mbk_bob"}


def test_middleware_recalls_once_per_user_message(fake: FakeMembase) -> None:
    model = search_then_answer()
    agent = create_agent(
        model,
        tools=membase_tools(fake.client()),
        system_prompt="You are helpful.",
        middleware=[MembaseRecallMiddleware(fake.client(), k=2)],
    )
    agent.invoke({"messages": [{"role": "user", "content": "What about the ledger?"}]})

    first, second = (system_text(m) for m in model.seen)
    assert first.startswith("You are helpful.\n\n## From the user's memory (Membase)")
    assert "- [Project decisions] Passage 1 about 'What about the ledger?'" in first
    assert second == first  # the model call after the tool call reuses the turn's recall
    searches = [r for r in fake.requests if r["path"] == "/v1/search"]
    assert [s["body"]["q"] for s in searches] == ["What about the ledger?", "ledger"]
    assert searches[0]["body"]["limit"] == 2
    assert "/v1/memories" not in fake.paths()  # nothing is written unless asked


def test_middleware_fails_open_within_its_budget(fake: FakeMembase, caplog) -> None:
    fake.search_delay = 1.5
    model = ScriptedModel(replies=[AIMessage(content="Hello!")])
    agent = create_agent(model, middleware=[MembaseRecallMiddleware(fake.client(), timeout=0.2)])
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="langchain_membase"):
        out = agent.invoke({"messages": [{"role": "user", "content": "hi"}]})
    assert time.monotonic() - started < 1.0
    assert out["messages"][-1].content == "Hello!"
    assert system_text(model.seen[0]) == ""
    assert "continuing without memory" in caplog.text and "0.2s" in caplog.text


def test_middleware_fails_open_on_revoked_key(fake: FakeMembase, caplog) -> None:
    fake.revoked_keys.add("mbk_alice")
    model = ScriptedModel(replies=[AIMessage(content="Hello!")])
    agent = create_agent(model, middleware=[MembaseRecallMiddleware(fake.client())])
    with caplog.at_level(logging.WARNING, logger="langchain_membase"):
        out = agent.invoke({"messages": [{"role": "user", "content": "hi"}]})
    assert out["messages"][-1].content == "Hello!"
    assert "narrowed or revoked" in caplog.text


def test_middleware_without_user_id_skips_recall(fake: FakeMembase, caplog) -> None:
    model = ScriptedModel(replies=[AIMessage(content="Hello!")])
    mw = MembaseRecallMiddleware(client_for=lambda u: fake.client(f"mbk_{u}"))
    with caplog.at_level(logging.WARNING, logger="langchain_membase"):
        create_agent(model, middleware=[mw]).invoke(
            {"messages": [{"role": "user", "content": "hi"}]}
        )
    assert fake.requests == [] and "user_id" in caplog.text


def test_capture_turns_is_opt_in(fake: FakeMembase) -> None:
    model = ScriptedModel(replies=[AIMessage(content="Noted, dark mode.")])
    mw = MembaseRecallMiddleware(fake.client(), container="mv-reading", capture_turns=True)
    create_agent(model, middleware=[mw]).invoke(
        {"messages": [{"role": "user", "content": "I like dark mode"}]}
    )
    deadline = time.monotonic() + 2
    while "/v1/memories" not in fake.paths() and time.monotonic() < deadline:
        time.sleep(0.01)
    saved = [r for r in fake.requests if r["path"] == "/v1/memories"]
    assert len(saved) == 1
    assert saved[0]["body"]["content"] == "I like dark mode"  # the user's words, no AI reply
    assert "Noted" not in saved[0]["body"]["content"]
    assert saved[0]["body"]["container"] == "mv-reading"


def test_capture_saves_only_the_users_messages_once_per_turn(fake: FakeMembase) -> None:
    model = ScriptedModel(replies=[AIMessage(content="Sure.")])
    mw = MembaseRecallMiddleware(fake.client(), container="mv-reading", capture_turns=True)
    agent = create_agent(model, middleware=[mw])
    history = [
        {"role": "user", "content": "old question", "id": "h0"},
        {"role": "assistant", "content": "old answer", "id": "a0"},
        {"role": "user", "content": "I moved to Lisbon.", "id": "h1"},
        {"role": "user", "content": "My new office is in Alfama.", "id": "h2"},
    ]
    mw.after_agent(agent.invoke({"messages": history}), None)  # same turn seen twice
    deadline = time.monotonic() + 2
    while "/v1/memories" not in fake.paths() and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    saved = [r["body"]["content"] for r in fake.requests if r["path"] == "/v1/memories"]
    assert saved == ["I moved to Lisbon.\n\nMy new office is in Alfama."]


async def test_async_agent_with_middleware(fake: FakeMembase) -> None:
    model = search_then_answer()
    agent = create_agent(
        model,
        tools=membase_tools(client_for=lambda u: fake.client(f"mbk_{u}")),
        middleware=[MembaseRecallMiddleware(client_for=lambda u: fake.client(f"mbk_{u}"))],
        context_schema=Ctx,
    )
    out = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "ledger?"}]}, context=Ctx("zoe")
    )
    assert out["messages"][-1].content == "You chose Postgres (Project decisions)."
    assert "## From the user's memory" in system_text(model.seen[0])
    assert {r["key"] for r in fake.requests} == {"mbk_zoe"}


async def test_async_middleware_fails_open(fake: FakeMembase) -> None:
    fake.search_delay = 1.5
    model = ScriptedModel(replies=[AIMessage(content="Hello!")])
    agent = create_agent(model, middleware=[MembaseRecallMiddleware(fake.client(), timeout=0.2)])
    started = time.monotonic()
    out = await agent.ainvoke({"messages": [{"role": "user", "content": "hi"}]})
    assert time.monotonic() - started < 1.0 and out["messages"][-1].content == "Hello!"


def test_capture_routes_to_the_users_key(fake: FakeMembase) -> None:
    model = ScriptedModel(replies=[AIMessage(content="Noted.")])
    mw = MembaseRecallMiddleware(
        client_for=lambda u: fake.client(f"mbk_{u}"), container="mv-reading", capture_turns=True
    )
    create_agent(model, middleware=[mw]).invoke(
        {"messages": [{"role": "user", "content": "remember: tea, not coffee"}]},
        config={"configurable": {"user_id": "frank"}},
    )
    deadline = time.monotonic() + 2
    while "/v1/memories" not in fake.paths() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert {r["key"] for r in fake.requests} == {"mbk_frank"}
    assert "/v1/memories" in fake.paths()
