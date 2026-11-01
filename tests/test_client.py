"""The client's own behaviour, against scripted transports (ported from the platform's SDK tests, whose hosted-app half stays in membase-platform), and how it picks hosted or local."""

from __future__ import annotations

import httpx
import pytest

import inspect
import json

from membase import (
    APIConnectionError,
    ConfirmationRequiredError,
    Membase,
    NotFoundError,
    RateLimitError,
    UnprocessableEntityError,
    WriteTargetRequiredError,
)


def _recording(answers: list[httpx.Response] | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers.pop(0) if answers else httpx.Response(200, json={})

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.example.test")
    return Membase("mbk_test", http_client=http, max_retries=0), seen


def _body(request: httpx.Request) -> dict | None:
    return json.loads(request.content) if request.content else None


def test_the_sdk_needs_a_key_and_reads_the_environment(monkeypatch):
    monkeypatch.delenv("MEMBASE_API_KEY", raising=False)
    monkeypatch.delenv("MEMBASE_LOCAL", raising=False)
    with pytest.raises(ValueError, match="MEMBASE_API_KEY"):
        Membase()
    monkeypatch.setenv("MEMBASE_API_KEY", "mbk_env")
    monkeypatch.setenv("MEMBASE_BASE_URL", "https://api.example.test/")
    c = Membase()
    assert c.api_key == "mbk_env" and c.base_url == "https://api.example.test"
    c.close()


def test_the_sdk_retries_429_and_5xx_then_raises_the_last_answer():
    answers = [
        httpx.Response(
            429,
            json={"error": {"code": "rate_limited", "message": "budget"}},
            headers={"Retry-After": "0"},
        ),
        httpx.Response(503, json={"error": {"code": "provider_unavailable", "message": "down"}}),
        httpx.Response(200, json={"actions": ["search"], "memories": [{"name": "A", "kind": "text"}]}),
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers.pop(0)

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.example.test")
    c = Membase("mbk_test", http_client=http, max_retries=2)
    assert c.capabilities()["actions"] == ["search"]
    assert len(seen) == 3
    assert seen[0].headers["authorization"] == "Bearer mbk_test"
    assert seen[0].headers["user-agent"].startswith("unibaseio-membase-python/")

    answers[:] = [
        httpx.Response(
            429,
            json={"error": {"code": "rate_limited", "message": "budget"}},
            headers={"Retry-After": "0"},
        ),
        httpx.Response(
            429,
            json={"error": {"code": "rate_limited", "message": "budget"}},
            headers={"Retry-After": "0"},
        ),
    ]
    c2 = Membase("mbk_test", http_client=http, max_retries=1)
    with pytest.raises(RateLimitError) as e:
        c2.capabilities()
    assert e.value.code == "rate_limited" and e.value.status == 429

    answers[:] = [
        httpx.Response(
            422,
            json={
                "error": {
                    "code": "capability_unavailable",
                    "message": "no model",
                    "retryable": True,
                }
            },
        ),
    ]
    with pytest.raises(UnprocessableEntityError) as e:
        Membase("mbk_test", http_client=http, max_retries=0).search("q")
    assert e.value.code == "capability_unavailable" and e.value.retryable is True


def test_the_sdk_turns_a_dead_connection_into_a_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.example.test")
    with pytest.raises(APIConnectionError):
        Membase("mbk_test", http_client=http, max_retries=1).capabilities()


def test_local_is_chosen_by_argument_or_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMBASE_API_KEY", raising=False)
    monkeypatch.setenv("MEMBASE_LOCAL", str(tmp_path / "env-store"))
    c = Membase()
    assert c.base_url == "http://membase.local" and c._http._transport.backend.root == tmp_path / "env-store"
    c.close()
    c = Membase(local=tmp_path / "arg-store")
    assert c._http._transport.backend.root == tmp_path / "arg-store"
    c.close()
    monkeypatch.setenv("MEMBASE_API_KEY", "mbk_env")
    c = Membase()
    assert c.base_url.startswith("https://")
    c.close()


def test_the_sdk_sends_the_memory_api_shapes_and_never_a_container():
    c, seen = _recording()
    c.search("ledger db")
    c.search("ledger db", limit=5, mode="fast")
    c.memories.list()
    c.memories.list(limit=3)
    c.memories.get("mem_ZGVmYXVsdDox")
    c.memories.add("We picked Postgres", title="ledger")
    c.memories.forget("mem_ZGVmYXVsdDox")
    c.memories.forget("mem_ZGVmYXVsdDox", confirm=True)
    c.documents.list(kind="note", limit=10)
    c.documents.get("doc_abc")
    c.documents.add("Design notes", custom_id="design-1")
    c.add(url="https://example.com/page", title="Page")
    c.documents.delete("doc_abc", confirm=True)
    c.media.list(limit=2)
    c.media.get("med_abc")
    c.media.search("someone skiing", kind="video", limit=4)
    c.media.delete("med_abc")
    c.profile("hours")
    c.rules()
    c.ask("Which database?")
    c.capabilities()

    calls = [(r.method, r.url.path, dict(r.url.params), _body(r)) for r in seen]
    assert calls == [
        ("POST", "/v1/search", {}, {"q": "ledger db", "mode": "full"}),
        ("POST", "/v1/search", {}, {"q": "ledger db", "mode": "fast", "limit": 5}),
        ("GET", "/v1/memories", {}, None),
        ("GET", "/v1/memories", {"limit": "3"}, None),
        ("GET", "/v1/memories/mem_ZGVmYXVsdDox", {}, None),
        ("POST", "/v1/memories/add", {}, {"content": "We picked Postgres", "title": "ledger"}),
        ("POST", "/v1/memories/mem_ZGVmYXVsdDox/forget", {"confirm": "false"}, None),
        ("POST", "/v1/memories/mem_ZGVmYXVsdDox/forget", {"confirm": "true"}, None),
        ("GET", "/v1/documents", {"kind": "note", "limit": "10"}, None),
        ("GET", "/v1/documents/doc_abc", {}, None),
        ("POST", "/v1/documents/add", {}, {"content": "Design notes", "title": "", "custom_id": "design-1"}),
        ("POST", "/v1/documents/add", {}, {"url": "https://example.com/page", "title": "Page", "custom_id": ""}),
        ("POST", "/v1/documents/doc_abc/delete", {"confirm": "true"}, None),
        ("GET", "/v1/media", {"limit": "2"}, None),
        ("GET", "/v1/media/med_abc", {}, None),
        ("POST", "/v1/media/search", {}, {"q": "someone skiing", "kind": "video", "limit": 4}),
        ("POST", "/v1/media/med_abc/delete", {"confirm": "false"}, None),
        ("GET", "/v1/profile", {"q": "hours"}, None),
        ("GET", "/v1/rules", {}, None),
        ("POST", "/v1/ask", {}, {"message": "Which database?"}),
        ("GET", "/v1/capabilities", {}, None),
    ]
    for r in seen:
        assert "container" not in str(r.url) and b"container" not in r.content


def test_no_method_takes_a_container():
    c, _ = _recording()
    methods = [c.add, c.search, c.memories.list, c.memories.add, c.memories.forget, c.documents.list,
               c.documents.add, c.documents.delete, c.media.list, c.media.search, c.media.add, c.media.ask]
    for method in methods:
        assert "container" not in inspect.signature(method).parameters, method
    assert not hasattr(c, "containers")
    with pytest.raises(TypeError):
        c.search("q", container="mv-1")  # type: ignore[call-arg]


def test_answers_come_back_as_the_server_sent_them():
    receipt = {"id": "doc_bm90ZQ", "kind": "note", "status": "processing",
               "learning": [{"memory_name": "Life", "status": "processing"}]}
    hits = {"items": [{"type": "memory", "id": "mem_eA", "content": "Postgres", "score": 1.0,
                       "memory_name": "Work", "title": "ledger"}],
            "partial": [{"memory_name": "Life", "reason": "error"}]}
    c, _ = _recording([httpx.Response(202, json=receipt), httpx.Response(200, json=hits)])
    assert c.memories.add("We picked Postgres") == receipt
    assert c.search("ledger") == hits


def test_a_removal_without_confirmation_is_a_typed_428():
    envelope = {"error": {"code": "confirmation_required", "message": "Pass confirm=true to forget this memory.",
                          "details": {"id": "mem_eA", "verb": "forget_memory", "content": "Postgres"},
                          "retryable": False, "trace_id": "t-1"}}
    c, seen = _recording([httpx.Response(428, json=envelope)])
    with pytest.raises(ConfirmationRequiredError) as e:
        c.memories.forget("mem_eA")
    assert e.value.status == 428 and e.value.code == "confirmation_required"
    assert e.value.details == {"id": "mem_eA", "verb": "forget_memory", "content": "Postgres"}
    assert e.value.trace_id == "t-1" and e.value.body == envelope
    assert len(seen) == 1  # a 428 is not retried


def test_a_write_without_a_write_target_is_a_typed_422():
    c, _ = _recording([
        httpx.Response(422, json={"error": {"code": "write_target_required", "message": "reaches 2 Memories",
                                            "details": {"memories_in_reach": 2}, "retryable": False}}),
        httpx.Response(422, json={"error": {"code": "validation", "message": "Request validation failed.",
                                            "details": {"fields": [{"loc": ["body", "container"]}]}}}),
        httpx.Response(404, json={"error": {"code": "not_found", "message": "memory not found",
                                            "details": {"id": "mem_x"}}}),
    ])
    with pytest.raises(WriteTargetRequiredError) as e:
        c.add("notes")
    assert isinstance(e.value, UnprocessableEntityError) and e.value.details == {"memories_in_reach": 2}
    with pytest.raises(UnprocessableEntityError) as e:
        c._request("POST", "/v1/search", json={"q": "x", "container": "mv-1"})
    assert not isinstance(e.value, WriteTargetRequiredError) and e.value.code == "validation"
    with pytest.raises(NotFoundError):
        c.memories.get("mem_x")
