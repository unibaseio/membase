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

CONTAINERS = [
    {"id": "mv-decisions", "name": "Project decisions"},
    {"id": "mv-reading", "name": "Reading notes"},
]


@dataclass
class FakeMembase:
    """Records every request; answers like the hosted API. Each key is one person's account."""

    requests: list[dict[str, Any]] = field(default_factory=list)
    containers: list[dict[str, Any]] = field(default_factory=lambda: list(CONTAINERS))
    search_delay: float = 0.0
    container_errors: dict[str, str] = field(default_factory=dict)
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
        if path == "/v1/containers":
            return httpx.Response(200, json={"containers": self.containers})
        if path == "/v1/search":
            if self.search_delay:
                time.sleep(self.search_delay)
            return httpx.Response(200, json=self._search(key, body))
        if path == "/v1/profile":
            out: dict[str, Any] = {
                "static": [f"The user behind {key} prefers dark mode."],
                "dynamic": [{"id": "1", "content": "Started reading Gödel, Escher, Bach."}],
            }
            if request.url.params.get("q"):
                out["results"] = self._search(key, {"q": request.url.params["q"], "limit": 1})[
                    "results"
                ]
            return httpx.Response(200, json=out)
        if path == "/v1/memories":
            if key in self.read_only_keys:
                return _error(
                    403,
                    "unauthorized",
                    "tool 'add_memory' is not in this agent's capability profile",
                )
            if body.get("static"):
                return httpx.Response(
                    202,
                    json={
                        "static": True,
                        "status": "recorded",
                        "run_id": "run-1",
                        "content": body["content"],
                    },
                )
            container = body.get("container")
            if container is None and len(self.containers) > 1:
                return _error(
                    400, "validation", "container is required when more than one is in reach"
                )
            container = container or self.containers[0]["id"]
            name = next(c["name"] for c in self.containers if c["id"] == container)
            return httpx.Response(
                202,
                json={
                    "memory_id": "m-1",
                    "document_id": "srcitem_1",
                    "status": "queued",
                    "container": container,
                    "container_name": name,
                    "learning_run": {"id": "r-1"},
                },
            )
        return _error(404, "not_found", f"no route {path}")

    def _search(self, key: str, body: dict[str, Any]) -> dict[str, Any]:
        q = body["q"]
        targets = [c for c in self.containers if body.get("container") in (None, c["id"])]
        limit = body.get("limit") or max(8, len(targets))
        answering = [c for c in targets if c["id"] not in self.container_errors]
        results = []
        if not self.no_results and answering:
            for i in range(limit):
                c = answering[i % len(answering)]
                results.append(
                    {
                        "content": f"Passage {i + 1} about {q!r} for {key}",
                        "container": c["id"],
                        "container_name": c["name"],
                        "score": 1.0 - i / 100,
                        "source": f"episode:{c['id']}:{i}",
                    }
                )
        return {
            "query": q,
            "results": results,
            "containers": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    **(
                        {"error": self.container_errors[c["id"]]}
                        if c["id"] in self.container_errors
                        else {}
                    ),
                }
                for c in targets
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
