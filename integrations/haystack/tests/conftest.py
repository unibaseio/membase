"""Offline fixtures: the real Membase SDK over an httpx.MockTransport, and a scripted chat generator."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
from haystack import component
from haystack.dataclasses import ChatMessage, ToolCall
from membase import Membase


class FakeMembase:
    """Answers the Membase REST calls the adapter makes and records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.search_delay = 0.0
        self.search_status = 200
        self.profile_status = 200
        self.memories_status = 202
        self.search_body: dict[str, Any] = {
            "items": [
                {
                    "type": "memory",
                    "id": "mem_eA",
                    "content": "We chose Postgres for the ledger.",
                    "memory_name": "Project decisions",
                    "score": 0.9,
                },
            ],
            "partial": [],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.params:
            body = {**body, **dict(request.url.params)}
        self.requests.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == "Bearer mbk_test"
        path = request.url.path
        if path == "/v1/search":
            if self.search_delay:
                time.sleep(self.search_delay)
            if self.search_status != 200:
                return _error(self.search_status)
            return httpx.Response(200, json=self.search_body)
        if path == "/v1/memories/add":
            if self.memories_status != 202:
                return _error(self.memories_status)
            return httpx.Response(
                202,
                json={
                    "id": "doc_bm90ZQ",
                    "kind": "note",
                    "status": "processing",
                    "learning": [{"memory_name": "Project decisions", "status": "processing"}],
                },
            )
        if path == "/v1/profile-facts":
            if self.profile_status != 200:
                return _error(self.profile_status)
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "pf_MQ", "content": "Works in Berlin", "source_kind": "user_stated"},
                        {"id": "pf_Mg", "content": "Started learning Go", "source_kind": "user_stated"},
                    ],
                    "has_more": False,
                    "partial": [],
                },
            )
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})

    def calls(self, path: str) -> list[dict[str, Any]]:
        return [body for _, p, body in self.requests if p == path]


def _error(status: int) -> httpx.Response:
    messages = {
        403: ("unauthorized", "unauthorized"),
        422: ("write_target_required", "this credential reaches 2 Memories and has no write target"),
    }
    code, message = messages.get(status, ("error", f"HTTP {status}"))
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


@pytest.fixture
def fake() -> FakeMembase:
    return FakeMembase()


@pytest.fixture
def client(fake: FakeMembase) -> Membase:
    http = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(fake.handler))
    return Membase(api_key="mbk_test", http_client=http, max_retries=0)


@component
class ScriptedChatGenerator:
    """A chat generator that replays scripted replies and records the messages it was sent."""

    def __init__(self, replies: list[ChatMessage]) -> None:
        self.replies = list(replies)
        self.seen: list[list[ChatMessage]] = []

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage], tools: Any = None, **kwargs: Any) -> dict[str, list[ChatMessage]]:
        self.seen.append(list(messages))
        return {"replies": [self.replies.pop(0)]}


def call(name: str, **args: Any) -> ChatMessage:
    return ChatMessage.from_assistant(tool_calls=[ToolCall(tool_name=name, arguments=args, id=f"call_{name}")])


def tool_results(messages: list[ChatMessage]) -> list[str]:
    return [m.tool_call_result.result for m in messages if m.tool_call_result is not None]
