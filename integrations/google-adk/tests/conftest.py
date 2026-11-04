"""Offline fixtures: the real Membase SDK over an httpx.MockTransport, and a scripted ADK model."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
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


class ScriptedLlm(BaseLlm):
    """An ADK model that replays scripted turns and records each request it was sent."""

    model: str = "scripted"
    turns: list[types.Content] = []
    seen: list[LlmRequest] = []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.seen.append(llm_request)
        yield LlmResponse(content=self.turns.pop(0))


def say(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def call(name: str, **args: Any) -> types.Content:
    return types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))])


def tool_responses(request: LlmRequest) -> list[dict[str, Any]]:
    """Every function response the model was sent in this request."""
    return [
        p.function_response.response for c in request.contents for p in c.parts or [] if p.function_response is not None
    ]


def request_text(request: LlmRequest) -> str:
    """Everything the model was told in this request: instructions and contents."""
    parts = [str(request.config.system_instruction or "")]
    parts += [p.text for c in request.contents for p in c.parts or [] if p.text]
    return "\n".join(parts)
