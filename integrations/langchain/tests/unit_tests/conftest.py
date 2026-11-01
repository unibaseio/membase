"""An offline Membase API: the real SDK, pointed at an httpx.MockTransport."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from membase import Membase

BASE_URL = "https://api.membase.test"

MEMORIES = ["Project decisions", "Reading notes"]


@dataclass
class FakeMembase:
    """Records every request; answers like the hosted memory API. Each key is one person's
    account; no request names a Memory."""

    requests: list[dict[str, Any]] = field(default_factory=list)
    memories: list[str] = field(default_factory=lambda: list(MEMORIES))
    write_target: str | None = "Project decisions"
    search_delay: float = 0.0
    memory_errors: set[str] = field(default_factory=set)
    revoked_keys: set[str] = field(default_factory=set)
    read_only_keys: set[str] = field(default_factory=set)
    no_results: bool = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        body = json.loads(request.content) if request.content else {}
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "key": key,
                "body": body,
                "params": dict(request.url.params),
            }
        )
        if key in self.revoked_keys:
            return _error(403, "unauthorized", "unauthorized")
        path = request.url.path
        if path == "/v1/search":
            if extra := set(body) - {"q", "limit", "mode"}:
                return _error(422, "validation", f"unknown fields: {sorted(extra)}")
            if self.search_delay:
                time.sleep(self.search_delay)
            return httpx.Response(200, json=self._search(key, body))
        if path == "/v1/capabilities":
            return httpx.Response(
                200,
                json={
                    "principal": "apikey",
                    "surfaces": ["rest"],
                    "tools": ["search_memories", "add_memory"],
                    "actions": ["search", "memories.add"],
                    "memories": [{"name": m, "kind": "text"} for m in self.memories],
                },
            )
        if path == "/v1/profile":
            out: dict[str, Any] = {
                "static": [f"The user behind {key} prefers dark mode."],
                "dynamic": [{"id": "1", "content": "Started reading Gödel, Escher, Bach."}],
            }
            if request.url.params.get("q"):
                # The profile's results keep the search hit shape that names container_name.
                out["results"] = [
                    {
                        "content": f"About {request.url.params['q']!r}",
                        "container_name": self.memories[0],
                    }
                ]
            return httpx.Response(200, json=out)
        if path == "/v1/memories/add" and request.method == "POST":
            if key in self.read_only_keys:
                return _error(
                    403,
                    "unauthorized",
                    "tool 'add_memory' is not in this agent's capability profile",
                )
            if extra := set(body) - {"content", "title"}:
                return _error(422, "validation", f"unknown fields: {sorted(extra)}")
            if self.write_target is None:
                return _error(
                    422,
                    "write_target_required",
                    f"reaches {len(self.memories)} Memories and has no write target",
                )
            return httpx.Response(
                202,
                json={
                    "id": "doc_bm90ZS0x",
                    "kind": "note",
                    "status": "processing",
                    "learning": [{"memory_name": self.write_target, "status": "processing"}],
                },
            )
        return _error(404, "not_found", f"no route {path}")

    def _search(self, key: str, body: dict[str, Any]) -> dict[str, Any]:
        q = body["q"]
        limit = body.get("limit") or 8
        answering = [m for m in self.memories if m not in self.memory_errors]
        items = []
        if not self.no_results and answering:
            for i in range(limit):
                name = answering[i % len(answering)]
                items.append(
                    {
                        "type": "memory",
                        "id": f"mem_{i}",
                        "content": f"Passage {i + 1} about {q!r} for {key}",
                        "score": 1.0 - i / 100,
                        "memory_name": name,
                    }
                )
        return {
            "items": items,
            "partial": [
                {"memory_name": m, "reason": "error"}
                for m in self.memories
                if m in self.memory_errors
            ],
        }

    def client(self, key: str = "mbk_alice") -> Membase:
        return Membase(
            api_key=key,
            base_url=BASE_URL,
            max_retries=0,
            http_client=httpx.Client(
                base_url=BASE_URL, transport=httpx.MockTransport(self.handler)
            ),
        )

    def paths(self) -> list[str]:
        return [r["path"] for r in self.requests]


def _error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


@pytest.fixture
def fake() -> FakeMembase:
    return FakeMembase()
