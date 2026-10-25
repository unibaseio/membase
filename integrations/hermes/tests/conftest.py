"""Test harness: a real Hermes checkout, the plugin installed as a user plugin, and a fake Membase API.

Hermes is imported from the virtualenv (``uv pip install -e .cache/hermes-agent``); the plugin is
installed the way ``hermes plugins install`` leaves it, as ``$HERMES_HOME/plugins/unibaseio-membase``, and
loaded by Hermes' own memory-provider loader. Every HTTP request any ``httpx.Client`` makes is
answered by an ``httpx.MockTransport``, so the real SDK runs end to end without a network.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

# Sandbox HERMES_HOME before anything imports hermes_cli (it attaches log handlers at import).
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="hermes-membase-tests-"))
os.environ["HERMES_HOME"] = str(_SESSION_HOME)
atexit.register(shutil.rmtree, _SESSION_HOME, ignore_errors=True)
for _var in ("MEMBASE_API_KEY", "MEMBASE_BASE_URL", "MEMBASE_CONTAINER", "MEMBASE_LOCAL"):
    os.environ.pop(_var, None)

import httpx  # noqa: E402
import pytest  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
KEY = "mbk_test_owner_key"
CONTAINERS = [
    {"id": "mv-decisions", "name": "Project decisions", "description": ""},
    {"id": "mv-travel", "name": "Travel", "description": ""},
]


class FakeMembase:
    """Just enough of the Membase REST API (POST /v1/search, GET /v1/profile, POST /v1/memories,
    GET /v1/containers). ``fail`` maps a path to a status code, an exception or a callable."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.containers = [dict(c) for c in CONTAINERS]
        self.search_response: dict[str, Any] = {
            "results": [
                {"content": "We settled on Postgres for the ledger.", "container_name": "Project decisions",
                 "container": "mv-decisions"},
                {"content": "Window seat, always.", "container_name": "Travel", "container": "mv-travel"},
            ],
            "containers": [{"id": "mv-decisions", "name": "Project decisions"}, {"id": "mv-travel", "name": "Travel"}],
        }
        self.profile_response: dict[str, Any] = {
            "static": ["Works on the ledger service."], "dynamic": ["Planning a trip to Lisbon."], "results": [],
        }
        self.fail: dict[str, Any] = {}
        self.delay: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- helpers for assertions --
    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    @staticmethod
    def body(request: httpx.Request) -> dict[str, Any]:
        return json.loads(request.content or b"{}")

    @staticmethod
    def timeout(request: httpx.Request) -> Optional[float]:
        return (request.extensions.get("timeout") or {}).get("read")

    def error(self, status: int, message: str, code: str = "unauthorized") -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": message, "retryable": False}})

    # -- the transport handler --
    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
        path = request.url.path
        if path in self.delay:
            time.sleep(self.delay[path])
        failure = self.fail.get(path)
        if isinstance(failure, BaseException):
            raise failure
        if callable(failure):
            return failure(request)
        if isinstance(failure, int):
            return self.error(failure, f"refused with {failure}")
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return self.error(401, "no bearer")
        if path == "/v1/containers" and request.method == "GET":
            return httpx.Response(200, json={"containers": self.containers})
        if path == "/v1/search" and request.method == "POST":
            return httpx.Response(200, json=self.search_response)
        if path == "/v1/profile" and request.method == "GET":
            return httpx.Response(200, json=self.profile_response)
        if path == "/v1/memories" and request.method == "POST":
            body = self.body(request)
            if body.get("static"):
                return httpx.Response(202, json={"static": True, "status": "recorded", "run_id": "run-1",
                                                 "content": body["content"]})
            if not body.get("container") and len(self.containers) > 1:
                return self.error(400, "several containers in reach: pass container", code="validation")
            return httpx.Response(202, json={"memory_id": "mem-1", "document_id": "srcitem_1", "status": "queued",
                                             "learning_run": {"id": "run-2"}})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})


@pytest.fixture
def fake(monkeypatch) -> FakeMembase:
    """Route every httpx.Client request in this process to a MockTransport over FakeMembase."""
    api = FakeMembase()
    mock = httpx.MockTransport(api)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request: mock.handle_request(request))
    return api


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A Hermes profile home with the plugin installed as a user plugin and selected as memory.provider."""
    h = tmp_path / "hermes-home"
    (h / "plugins").mkdir(parents=True)
    (h / "plugins" / "unibaseio-membase").symlink_to(PLUGIN_ROOT, target_is_directory=True)
    (h / "config.yaml").write_text("memory:\n  provider: unibaseio-membase\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("MEMBASE_API_KEY", KEY)
    for var in ("MEMBASE_BASE_URL", "MEMBASE_CONTAINER"):
        monkeypatch.delenv(var, raising=False)
    return h


@pytest.fixture
def load():
    """Load the provider through Hermes' memory-plugin loader (what agent init does)."""

    def _load():
        from plugins.memory import load_memory_provider

        provider = load_memory_provider("unibaseio-membase")
        assert provider is not None, "Hermes did not load the unibaseio-membase provider from $HERMES_HOME/plugins"
        return provider

    return _load


@pytest.fixture
def manager(home, load, fake):
    """A MemoryManager with the loaded provider, initialized like a CLI agent's."""
    from agent.memory_manager import MemoryManager

    made: list[Any] = []

    def _make(*, provider=None, timeout: Optional[float] = None, **init_kwargs: Any):
        mgr = MemoryManager(external_prefetch_timeout=timeout) if timeout else MemoryManager()
        p = provider or load()
        assert p.is_available()
        mgr.add_provider(p)
        kwargs = {"platform": "cli", "hermes_home": str(home), "agent_context": "primary", **init_kwargs}
        mgr.initialize_all(session_id="s-1", **kwargs)
        made.append(mgr)
        return mgr, p

    yield _make
    for mgr in made:
        mgr.shutdown_all()
