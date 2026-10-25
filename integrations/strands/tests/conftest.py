"""Offline fixtures: the real Membase SDK over an httpx.MockTransport, and a scripted Strands model."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

import httpx
import pytest
from membase import Membase
from strands.models import Model

CONTAINERS = [
    {"id": "mv-decisions", "name": "Project decisions"},
    {"id": "mv-health", "name": "Health"},
]


class FakeMembase:
    """Answers the Membase REST calls the adapter makes and records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.search_delay = 0.0
        self.search_status = 200
        self.profile_status = 200
        self.memories_status = 202
        self.search_body: dict[str, Any] = {
            "results": [
                {"content": "We chose Postgres for the ledger.", "container_name": "Project decisions", "score": 0.9},
            ],
            "containers": [{"id": "mv-decisions", "name": "Project decisions"}],
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
        if path == "/v1/containers":
            return httpx.Response(200, json={"containers": CONTAINERS})
        if path == "/v1/memories":
            if self.memories_status != 202:
                return _error(self.memories_status)
            if body.get("static"):
                return httpx.Response(202, json={"static": True, "status": "recorded", "content": body["content"]})
            return httpx.Response(202, json={"memory_id": "m1", "document_id": "d1", "status": "queued"})
        if path == "/v1/profile":
            if self.profile_status != 200:
                return _error(self.profile_status)
            return httpx.Response(
                200,
                json={"static": ["Works in Berlin"], "dynamic": ["Started learning Go"], "results": []},
            )
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})

    def calls(self, path: str) -> list[dict[str, Any]]:
        return [body for _, p, body in self.requests if p == path]


def _error(status: int) -> httpx.Response:
    messages = {
        400: ("validation", "container is required when more than one is in reach"),
        403: ("unauthorized", "unauthorized"),
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


class ScriptedModel(Model):
    """A Strands model that replays scripted assistant turns and records what it was sent."""

    def __init__(self, turns: list[dict[str, Any] | Callable[[list], dict[str, Any]]]) -> None:
        self.turns = list(turns)
        self.seen: list[list[dict[str, Any]]] = []
        self.tool_names: list[list[str]] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> Any:
        return {}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):  # pragma: no cover
        raise NotImplementedError
        yield  # noqa: B901

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs) -> AsyncGenerator[Any, None]:
        self.seen.append(json.loads(json.dumps(messages, default=str)))
        self.tool_names.append([spec["name"] for spec in tool_specs or []])
        turn = self.turns.pop(0)
        if callable(turn):
            turn = turn(messages)
        stop = "end_turn"
        yield {"messageStart": {"role": "assistant"}}
        for block in turn["content"]:
            if "text" in block:
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": {"text": block["text"]}}}
                yield {"contentBlockStop": {}}
            if "toolUse" in block:
                stop = "tool_use"
                use = block["toolUse"]
                yield {
                    "contentBlockStart": {"start": {"toolUse": {"name": use["name"], "toolUseId": use["toolUseId"]}}}
                }
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(use["input"])}}}}
                yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": stop}}


def text(t: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"text": t}]}


def tool_use(name: str, args: dict[str, Any], use_id: str = "t1") -> dict[str, Any]:
    return {"role": "assistant", "content": [{"toolUse": {"name": name, "toolUseId": use_id, "input": args}}]}


def last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message["role"] == "user":
            return "\n".join(b.get("text", "") for b in message["content"] if "text" in b)
    return ""


def tool_result_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        for block in message["content"]:
            if "toolResult" in block:
                return "\n".join(c.get("text", "") for c in block["toolResult"]["content"] if "text" in c)
    return ""
