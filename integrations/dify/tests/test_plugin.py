"""The plugin as Dify runs it: manifest + YAML loaded by dify_plugin's PluginRegistration, calls
dispatched by its PluginExecutor, and the Membase SDK answering from an httpx.MockTransport."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin"
KEY = "mbk_test_secret_123456"


@pytest.fixture(scope="session")
def executor():
    """Load the plugin the way the runtime does: from its own directory, by its manifest."""
    from dify_plugin.config.config import DifyPluginEnv
    from dify_plugin.core.plugin_executor import PluginExecutor
    from dify_plugin.core.plugin_registration import PluginRegistration

    cwd = os.getcwd()
    sys.path.insert(0, str(PLUGIN_DIR))
    os.chdir(PLUGIN_DIR)
    try:
        config = DifyPluginEnv()
        registration = PluginRegistration(config)
    finally:
        os.chdir(cwd)
    return PluginExecutor(config, registration)


class FakeMembase:
    """Answers the Membase REST calls the SDK makes and records them."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.containers = [
            {"id": "mv-decisions", "name": "Project decisions"},
            {"id": "mv-travel", "name": "Travel"},
        ]
        self.routes: dict[tuple[str, str], object] = {}

    def on(self, method: str, path: str, answer) -> None:
        self.routes[(method, path)] = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes.get((request.method, request.url.path))
        if callable(answer):
            return answer(request)
        if answer is not None:
            status, body = answer
            return httpx.Response(status, json=body)
        if (request.method, request.url.path) == ("GET", "/v1/containers"):
            return httpx.Response(200, json={"containers": self.containers})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no route"}})

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


@pytest.fixture
def membase(executor, monkeypatch):
    import membase.client as sdk_client

    import membase_dify.client as plugin_client

    fake = FakeMembase()
    seen_base_urls: list[str] = []

    def http_client(base_url: str) -> httpx.Client:
        seen_base_urls.append(base_url)
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(fake))

    monkeypatch.setattr(plugin_client, "_http_client", http_client)
    monkeypatch.setattr(sdk_client.time, "sleep", lambda _s: None)
    fake.base_urls = seen_base_urls
    return fake


def invoke(executor, tool: str, params: dict, credentials: dict | None = None):
    from dify_plugin.core.entities.plugin.request import ToolInvokeRequest
    from dify_plugin.core.runtime import Session

    request = ToolInvokeRequest(
        user_id="dify-end-user",
        provider="membase",
        tool=tool,
        credentials=credentials if credentials is not None else {"api_key": KEY},
        tool_parameters=params,
    )
    messages = list(executor.invoke_tool(Session.empty_session(), request))
    text = "\n".join(m.message.text for m in messages if m.type.value == "text")
    data = [m.message.json_object for m in messages if m.type.value == "json"]
    return text, (data[0] if data else None)


def validate(executor, credentials: dict):
    from dify_plugin.core.entities.plugin.request import ToolValidateCredentialsRequest
    from dify_plugin.core.runtime import Session

    request = ToolValidateCredentialsRequest(
        user_id="admin", provider="membase", credentials=credentials
    )
    return executor.validate_tool_provider_credentials(Session.empty_session(), request)


def body(request: httpx.Request) -> dict:
    return json.loads(request.content or b"{}")


# ---------------------------------------------------------------- registration


def test_runtime_registers_exactly_the_three_tools(executor):
    provider, _cls, tools = executor.registration.tools_mapping["membase"]
    assert sorted(tools) == ["get_profile", "save_memory", "search_memory"]
    schema = {c.name: c for c in provider.credentials_schema}
    assert schema["api_key"].type.value == "secret-input" and schema["api_key"].required
    assert schema["base_url"].type.value == "text-input" and not schema["base_url"].required
    for config, _tool_cls in tools.values():
        assert config.identity.label.zh_hans != config.identity.label.en_us
        assert config.description.llm.strip()
        for p in config.parameters:
            assert p.label.zh_hans != p.label.en_us  # zh_Hans written, not defaulted
            # No parameter can ask Membase to delete or to confirm anything.
            assert p.name not in {"confirm", "delete", "forget"}


def test_manifest_points_at_privacy_icon_and_declares_domains():
    import yaml

    manifest = yaml.safe_load((PLUGIN_DIR / "manifest.yaml").read_text())
    assert manifest["author"] == "unibase" and manifest["name"] == "membase"
    assert (PLUGIN_DIR / manifest["privacy"]).is_file()
    assert (PLUGIN_DIR / "_assets" / manifest["icon"]).is_file()
    assert "api.app.membase.com" in manifest["network"]["domains"]
    assert manifest["repo"].startswith("https://github.com/")


# ---------------------------------------------------------------- credentials


def test_validation_lists_containers_with_the_key(executor, membase):
    assert validate(executor, {"api_key": f"  {KEY} "}) == {"result": True}
    (call,) = membase.calls("GET", "/v1/containers")
    assert call.headers["authorization"] == f"Bearer {KEY}"
    assert membase.base_urls == ["https://api.app.membase.com"]


def test_validation_surfaces_403_without_echoing_the_key(executor, membase):
    from dify_plugin.errors.tool import ToolProviderCredentialValidationError

    membase.on(
        "GET",
        "/v1/containers",
        (403, {"error": {"code": "unauthorized", "message": "invalid or revoked key"}}),
    )
    with pytest.raises(ToolProviderCredentialValidationError) as err:
        validate(executor, {"api_key": KEY})
    message = str(err.value)
    assert "403" in message and "revoked" in message and "Developer keys" in message
    assert KEY not in message
    assert len(membase.calls("GET", "/v1/containers")) == 1  # a 403 is never retried


def test_validation_requires_a_key(executor, membase):
    from dify_plugin.errors.tool import ToolProviderCredentialValidationError

    with pytest.raises(ToolProviderCredentialValidationError, match="No Membase API key"):
        validate(executor, {"api_key": "   "})
    assert membase.requests == []


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.app.membase.ai",
        "https://evil.example.com",
        "https://membase.ai.evil.com",
        "https://user:pw@api.app.membase.ai",
        "https://api.app.membase.ai/v1",
        "https://api.app.membase.ai:8443",
        "https://169.254.169.254",
    ],
)
def test_validation_refuses_a_base_url_off_membase(executor, membase, base_url):
    from dify_plugin.errors.tool import ToolProviderCredentialValidationError

    with pytest.raises(ToolProviderCredentialValidationError, match="Base URL"):
        validate(executor, {"api_key": KEY, "base_url": base_url})
    assert membase.requests == []


def test_a_membase_base_url_is_used(executor, membase):
    validate(executor, {"api_key": KEY, "base_url": "https://api.app.membase.io/"})
    assert membase.base_urls == ["https://api.app.membase.io"]
    assert membase.requests[0].url.host == "api.app.membase.io"


# ---------------------------------------------------------------- search_memory


SEARCH_ANSWER = {
    "query": "database",
    "results": [
        {
            "content": "We settled on Postgres for the ledger.",
            "score": 0.9,
            "container": "mv-decisions",
            "container_name": "Project decisions",
            "source": "12",
        },
        {
            "content": "Flights to Lisbon on the 3rd.",
            "score": 0.4,
            "container": "mv-travel",
            "container_name": "Travel",
            "source": "7",
        },
    ],
    "containers": [
        {"id": "mv-decisions", "name": "Project decisions"},
        {"id": "mv-travel", "name": "Travel"},
    ],
}


def test_search_cites_each_container(executor, membase):
    membase.on("POST", "/v1/search", (200, SEARCH_ANSWER))
    text, data = invoke(executor, "search_memory", {"query": "database", "limit": 5})
    assert "1. [Project decisions] We settled on Postgres for the ledger." in text
    assert "2. [Travel] Flights to Lisbon on the 3rd." in text
    assert [r["container_name"] for r in data["results"]] == ["Project decisions", "Travel"]
    (call,) = membase.calls("POST", "/v1/search")
    assert body(call) == {"q": "database", "limit": 5}
    assert membase.calls("GET", "/v1/containers") == []  # no container named, no lookup


def test_search_resolves_a_container_name_to_its_id(executor, membase):
    membase.on(
        "POST", "/v1/search", (200, {**SEARCH_ANSWER, "results": SEARCH_ANSWER["results"][:1]})
    )
    invoke(executor, "search_memory", {"query": "database", "container": "project DECISIONS"})
    assert body(membase.calls("POST", "/v1/search")[0]) == {
        "q": "database",
        "container": "mv-decisions",
    }


def test_search_names_the_containers_when_one_is_unknown(executor, membase):
    from membase_dify.client import MembaseToolError

    with pytest.raises(MembaseToolError) as err:
        invoke(executor, "search_memory", {"query": "x", "container": "Recipes"})
    assert "Project decisions (mv-decisions)" in str(err.value)
    assert membase.calls("POST", "/v1/search") == []


def test_search_reports_a_container_that_could_not_answer(executor, membase):
    membase.on(
        "POST",
        "/v1/search",
        (
            200,
            {
                "results": [],
                "containers": [
                    {"id": "mv-decisions", "name": "Project decisions", "error": "no working model"}
                ],
            },
        ),
    )
    text, data = invoke(executor, "search_memory", {"query": "database"})
    assert "No memories matched." in text
    assert "Could not search container 'Project decisions': no working model" in text
    assert "not the same as no match" in text
    assert data["unavailable_containers"][0]["error"] == "no working model"


def test_search_403_is_reported_and_not_retried(executor, membase):
    from membase_dify.client import MembaseToolError

    membase.on(
        "POST",
        "/v1/search",
        (403, {"error": {"code": "unauthorized", "message": "may not use that container"}}),
    )
    with pytest.raises(MembaseToolError) as err:
        invoke(executor, "search_memory", {"query": "database"})
    assert "403: may not use that container" in str(err.value)
    assert "revoked or narrowed" in str(err.value)
    assert len(membase.calls("POST", "/v1/search")) == 1


def test_search_timeout_explains_the_cold_start(executor, membase):
    from membase_dify.client import MembaseToolError

    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    membase.on("POST", "/v1/search", slow)
    with pytest.raises(MembaseToolError, match="up to a minute"):
        invoke(executor, "search_memory", {"query": "database"})
    assert len(membase.calls("POST", "/v1/search")) == 2  # one retry, within Dify's budget


def test_search_requires_a_query(executor, membase):
    from membase_dify.client import MembaseToolError

    with pytest.raises(MembaseToolError, match="query is required"):
        invoke(executor, "search_memory", {"query": "  "})
    assert membase.requests == []


# ---------------------------------------------------------------- save_memory


def test_save_to_a_named_container(executor, membase):
    membase.on(
        "POST",
        "/v1/memories",
        (202, {"memory_id": "m1", "document_id": "srcitem_1", "status": "queued"}),
    )
    text, data = invoke(
        executor,
        "save_memory",
        {"content": "We settled on Postgres.", "container": "Project decisions"},
    )
    (call,) = membase.calls("POST", "/v1/memories")
    assert body(call) == {
        "content": "We settled on Postgres.",
        "static": False,
        "title": "",
        "container": "mv-decisions",
    }
    assert "Saved to Project decisions (status: queued)" in text
    assert data["container_name"] == "Project decisions" and data["status"] == "queued"


def test_save_a_profile_fact(executor, membase):
    membase.on("POST", "/v1/memories", (202, {"static": True, "status": "recorded"}))
    text, data = invoke(
        executor,
        "save_memory",
        {"content": "I prefer dark mode.", "static": "true", "container": "Travel"},
    )
    (call,) = membase.calls("POST", "/v1/memories")
    assert body(call) == {"content": "I prefer dark mode.", "static": True, "title": ""}
    assert membase.calls("GET", "/v1/containers") == []
    assert "profile" in text and data["status"] == "recorded"


def test_save_without_a_container_lists_the_choices(executor, membase):
    from membase_dify.client import MembaseToolError

    membase.on(
        "POST",
        "/v1/memories",
        (400, {"error": {"code": "validation", "message": "container is required"}}),
    )
    with pytest.raises(MembaseToolError) as err:
        invoke(executor, "save_memory", {"content": "Note this."})
    assert "container is required" in str(err.value)
    assert "Project decisions (mv-decisions), Travel (mv-travel)" in str(err.value)
    assert "container" not in body(membase.calls("POST", "/v1/memories")[0])


def test_save_with_a_read_only_key_is_refused(executor, membase):
    from membase_dify.client import MembaseToolError

    membase.on(
        "POST",
        "/v1/memories",
        (
            403,
            {
                "error": {
                    "code": "unauthorized",
                    "message": "not in this agent's capability profile",
                }
            },
        ),
    )
    with pytest.raises(MembaseToolError) as err:
        invoke(executor, "save_memory", {"content": "x", "container": "mv-travel"})
    assert "Read key cannot save" in str(err.value)
    assert len(membase.calls("POST", "/v1/memories")) == 1


def test_save_requires_content(executor, membase):
    from membase_dify.client import MembaseToolError

    with pytest.raises(MembaseToolError, match="content is required"):
        invoke(executor, "save_memory", {"content": ""})
    assert membase.requests == []


# ---------------------------------------------------------------- get_profile


PROFILE = {
    "available": True,
    "ready": True,
    "static": ["Name is Ada.", "Prefers dark mode."],
    "dynamic": [{"id": "3", "content": "Working on the ledger.", "updated_at": "2026-10-19"}],
}


def test_profile_lists_standing_and_recent_facts(executor, membase):
    membase.on("GET", "/v1/profile", (200, PROFILE))
    text, data = invoke(executor, "get_profile", {})
    assert "Standing facts:\n- Name is Ada.\n- Prefers dark mode." in text
    assert "Recently changed:\n- Working on the ledger." in text
    assert data == PROFILE
    assert membase.calls("GET", "/v1/profile")[0].url.params.get("q") is None


def test_profile_with_a_topic_cites_containers(executor, membase):
    membase.on(
        "GET",
        "/v1/profile",
        (200, {**PROFILE, "results": SEARCH_ANSWER["results"][:1]}),
    )
    text, _ = invoke(executor, "get_profile", {"query": "database"})
    assert "- [Project decisions] We settled on Postgres for the ledger." in text
    assert membase.calls("GET", "/v1/profile")[0].url.params["q"] == "database"


def test_profile_not_granted(executor, membase):
    from membase_dify.client import MembaseToolError

    membase.on(
        "GET",
        "/v1/profile",
        (
            403,
            {
                "error": {
                    "code": "unauthorized",
                    "message": "tool 'get_profile' is not in this agent's capability profile",
                }
            },
        ),
    )
    with pytest.raises(MembaseToolError, match="did not grant the profile"):
        invoke(executor, "get_profile", {})


def test_profile_that_could_not_be_read_says_so(executor, membase):
    membase.on(
        "GET",
        "/v1/profile",
        (200, {"available": False, "reason": "no agent container", "static": [], "dynamic": []}),
    )
    text, _ = invoke(executor, "get_profile", {})
    assert text == "The profile could not be read right now: no agent container."


def test_the_membase_com_api_host_is_accepted(executor, membase):
    validate(executor, {"api_key": KEY, "base_url": "https://api.app.membase.com"})
    assert membase.base_urls == ["https://api.app.membase.com"]
    assert membase.requests[0].url.host == "api.app.membase.com"
