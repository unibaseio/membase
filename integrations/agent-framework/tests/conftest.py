"""Offline fixtures: a fake Membase API behind ``httpx.MockTransport`` and a scripted chat
client that the real ``agent_framework.Agent`` drives (tool calls included)."""

from __future__ import annotations

import json
import time
from collections.abc import MutableSequence
from typing import Any

import httpx
import pytest
from agent_framework import (
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
)
from membase import Membase


class FakeMembase:
    """Answers the REST calls the SDK makes; records each request with its bearer."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.write_target: str | None = "Travel"  # None: the key reaches several Memories
        self.results: list[dict[str, Any]] = [
            {
                "type": "memory",
                "id": "mem_bGVkZ2Vy",
                "content": "We settled on Postgres for the ledger.",
                "score": 1.0,
                "memory_name": "Project decisions",
            },
        ]
        self.partial: list[dict[str, Any]] = []
        self.status: dict[str, int] = {}  # path -> forced status
        self.delay: float = 0.0
        self.profile = {"static": ["Works in Berlin"], "dynamic": ["Prefers dark mode"], "results": []}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "params": dict(request.url.params),
                "json": body,
                "auth": request.headers.get("authorization"),
            }
        )
        if self.delay:
            time.sleep(self.delay)
        path = request.url.path
        forced = self.status.get(path)
        if forced:
            return httpx.Response(
                forced, json={"error": {"code": "unauthorized", "message": f"forced {forced}"}}
            )
        if path == "/v1/search":
            if extra := set(body) - {"q", "limit", "mode"}:
                return _error(422, "validation", f"unknown fields: {sorted(extra)}")
            return httpx.Response(200, json={"items": self.results, "partial": self.partial})
        if path == "/v1/profile":
            return httpx.Response(200, json=self.profile)
        if path == "/v1/memories" and request.method == "POST":
            if extra := set(body) - {"content", "title"}:
                return _error(422, "validation", f"unknown fields: {sorted(extra)}")
            if self.write_target is None:
                return _error(422, "write_target_required", "reaches 2 Memories and has no write target")
            return httpx.Response(
                202,
                json={
                    "id": "doc_bm90ZQ",
                    "kind": "note",
                    "status": "processing",
                    "learning": [{"memory_name": self.write_target, "status": "processing"}],
                },
            )
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})

    def client(self, key: str = "mbk_test") -> Membase:
        http = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(self.handler))
        return Membase(api_key=key, http_client=http, max_retries=0)

    def calls(self, path: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"] == path]


def _error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


class ScriptedChatClient(FunctionInvocationLayer[Any], BaseChatClient[Any]):
    """A chat client with no model: replays scripted responses and records every request."""

    def __init__(self, script: list[ChatResponse] | None = None) -> None:
        super().__init__()
        self.script = list(script or [])
        self.seen: list[dict[str, Any]] = []

    def _inner_get_response(  # type: ignore[override]
        self, *, messages: MutableSequence[Message], stream: bool, options: dict[str, Any], **kwargs: Any
    ) -> Any:
        async def _get() -> ChatResponse:
            self.seen.append({"messages": list(messages), "options": dict(options)})
            if self.script:
                return self.script.pop(0)
            return ChatResponse(messages=Message(role="assistant", contents=["ok"]))

        return _get()


def call(name: str, args: dict[str, Any], call_id: str = "c1") -> ChatResponse:
    return ChatResponse(
        messages=Message(
            role="assistant",
            contents=[Content.from_function_call(call_id=call_id, name=name, arguments=args)],
        )
    )


def say(text: str) -> ChatResponse:
    return ChatResponse(messages=Message(role="assistant", contents=[text]))


def tool_results(chat: ScriptedChatClient) -> list[str]:
    """Text of every function result the model was shown, in order."""
    out: list[str] = []
    for message in chat.seen[-1]["messages"]:
        for c in message.contents:
            if c.type == "function_result":
                result = c.result
                if isinstance(result, list):
                    result = "".join(getattr(x, "text", "") or "" for x in result)
                out.append(str(result))
    return out


@pytest.fixture
def mem() -> FakeMembase:
    return FakeMembase()
