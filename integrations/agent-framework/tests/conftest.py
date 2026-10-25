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
        self.containers = [
            {"id": "mv-decisions", "name": "Project decisions"},
            {"id": "mv-travel", "name": "Travel"},
        ]
        self.results: list[dict[str, Any]] = [
            {"content": "We settled on Postgres for the ledger.", "container_name": "Project decisions"},
        ]
        self.container_errors: list[dict[str, Any]] = []
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
            return httpx.Response(200, json={"results": self.results, "containers": self.container_errors})
        if path == "/v1/containers":
            return httpx.Response(200, json={"containers": self.containers})
        if path == "/v1/profile":
            return httpx.Response(200, json=self.profile)
        if path == "/v1/memories":
            if body.get("static"):
                return httpx.Response(
                    202, json={"static": True, "status": "recorded", "content": body["content"]}
                )
            if body.get("container") is None and len(self.containers) > 1:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "code": "validation",
                            "message": "container is required with several in reach",
                        }
                    },
                )
            return httpx.Response(
                202, json={"memory_id": "m1", "document_id": "d1", "status": "queued", "learning_run": None}
            )
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})

    def client(self, key: str = "mbk_test") -> Membase:
        http = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(self.handler))
        return Membase(api_key=key, http_client=http, max_retries=0)

    def calls(self, path: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"] == path]


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
