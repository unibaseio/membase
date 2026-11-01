"""Local memory answers the hosted memory API: shapes, refusals and confirmations offline (the
engine's model calls replaced by a stand-in), and one round trip through the real engine and model
provider when a key is configured."""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("membase_core", reason="needs the local extra (unibaseio-membase-core, Python 3.12+)")

from membase import (  # noqa: E402
    BadRequestError,
    ConfirmationRequiredError,
    Membase,
    NotFoundError,
    UnprocessableEntityError,
    WriteTargetRequiredError,
)
from membase.local import ids  # noqa: E402
from membase.local.backend import LocalBackend  # noqa: E402
from membase.local.server import make_server  # noqa: E402

EMPTY_LIST = {"items": [], "has_more": False, "partial": []}


class FakeEngine:
    """The engine's store without its model calls: a fact per turn, a document per ingest, and a
    search that matches words."""

    def __init__(self) -> None:
        self.episodes_: list[dict] = []
        self.docs: dict[str, SimpleNamespace] = {}
        self._lock = threading.RLock()

    def ingest_session(self, sid, date, turns, episode_owner=None):
        for t in turns:
            n = max((e["id"] for e in self.episodes_), default=0) + 1
            self.episodes_.append({"id": n, "content": t["content"], "created_at": date, "title": "",
                                   "session_date": date, "session_id": sid})
        return SimpleNamespace(counts={"episodes": len(turns)})

    def episodes(self, *, limit=20, owner=""):
        return sorted(self.episodes_, key=lambda e: -e["id"])[:limit]

    def forget(self, eid):
        self.episodes_ = [e for e in self.episodes_ if e["id"] != eid]
        return True

    def search(self, q, query_date=None, *, owner=None):
        words = set(q.lower().split())
        return SimpleNamespace(observations_top=[
            SimpleNamespace(id=e["id"], text=e["content"], score=1.0, rrf=0.5, subject="ledger",
                            valid_at=e["session_date"])
            for e in self.episodes_ if words & set(e["content"].lower().split())
        ])

    def ingest_document(self, path=None, *, text=None, title=None, doc_id=None, **_):
        from membase_core import DuplicateDocumentError

        doc_id = doc_id or f"d{len(self.docs) + 1}"
        if doc_id in self.docs:
            raise DuplicateDocumentError(doc_id)
        self.docs[doc_id] = SimpleNamespace(doc_id=doc_id, title=title or "", created_at="2026-11-01T00:00:00Z",
                                            text=text, summary="", topics=[])
        return SimpleNamespace(doc_id=doc_id, topic_count=1)

    def list_documents(self, *, page=1, page_size=20, **_):
        docs = list(self.docs.values())
        return SimpleNamespace(total=len(docs), documents=docs[(page - 1) * page_size:page * page_size])

    def get_document(self, doc_id):
        from membase_core import DocumentNotFoundError

        if doc_id not in self.docs:
            raise DocumentNotFoundError(doc_id)
        return self.docs[doc_id]

    def delete_document(self, doc_id):
        self.docs.pop(doc_id)

    def search_knowledge(self, q, *, top_k=10, include_content=False, **_):
        words = set(q.lower().split())
        return SimpleNamespace(hits=[
            SimpleNamespace(content=d.text, summary="", score=0.6, document=d, topic_id="t1", topic_path=d.title)
            for d in self.docs.values() if words & set((d.text or "").lower().split())
        ])

    def save(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMBASE_WRITE_TARGET", raising=False)
    m = Membase(local=tmp_path / "store")
    yield m
    m.close()


@pytest.fixture
def faked(local, monkeypatch):
    """Local memory whose engines are stand-ins, one per container."""
    engines: dict[str, FakeEngine] = {}

    def engine(self, cid):
        self._dir(cid).mkdir(parents=True, exist_ok=True)
        e = engines.setdefault(cid, FakeEngine())
        return e, e._lock

    monkeypatch.setattr(LocalBackend, "engine", engine)
    return local


def test_an_empty_store_lists_nothing(local):
    assert local.search("anything") == {"items": [], "partial": []}
    assert local.memories.list() == EMPTY_LIST
    assert local.documents.list() == EMPTY_LIST
    assert local.media.list() == EMPTY_LIST
    assert local.rules()["rules"] == []
    caps = local.capabilities()
    assert {"search", "memories.list", "memories.add", "memories.forget", "documents.add"} <= set(caps["actions"])
    assert caps["memories"] == []


def test_a_fact_round_trip(faked):
    receipt = faked.memories.add("We picked Postgres for the ledger", title="ledger")
    assert receipt["kind"] == "note" and receipt["id"].startswith("doc_")
    assert receipt["status"] == "succeeded"
    assert receipt["learning"] == [{"memory_name": "default", "status": "succeeded"}]

    hits = faked.search("postgres ledger")
    assert hits["partial"] == []
    [hit] = hits["items"]
    assert hit["type"] == "memory" and hit["id"].startswith("mem_") and hit["memory_name"] == "default"
    assert "container" not in hit and "container_name" not in hit

    listed = faked.memories.list()
    assert listed["has_more"] is False and [m["id"] for m in listed["items"]] == [hit["id"]]
    one = faked.memories.get(hit["id"])
    assert set(one) == {"id", "content", "title", "memory_name", "source_time", "recorded_at"} - {"title"}
    assert one["content"].startswith("ledger") and one["memory_name"] == "default"

    notes = faked.documents.list(kind="note")
    assert [d["id"] for d in notes["items"]] == [receipt["id"]]
    assert faked.documents.list(kind="file") == EMPTY_LIST
    doc = faked.documents.get(receipt["id"])
    assert doc["kind"] == "note" and doc["title"] == "ledger" and doc["learning"][0]["status"] == "succeeded"

    with pytest.raises(ConfirmationRequiredError) as e:
        faked.memories.forget(hit["id"])
    assert e.value.status == 428 and e.value.code == "confirmation_required"
    assert e.value.details["id"] == hit["id"] and e.value.details["verb"] == "forget_memory"
    assert faked.memories.list()["items"]  # nothing changed
    assert faked.memories.forget(hit["id"], confirm=True) == {"id": hit["id"], "status": "forgotten"}
    assert faked.memories.list() == EMPTY_LIST
    with pytest.raises(NotFoundError):
        faked.memories.get(hit["id"])
    assert faked.documents.get(receipt["id"])["id"] == receipt["id"]  # the note it came from stays


def test_deleting_a_note_withdraws_what_it_taught(faked):
    receipt = faked.memories.add("My sister lives in Lisbon")
    assert faked.memories.list()["items"]
    with pytest.raises(ConfirmationRequiredError) as e:
        faked.documents.delete(receipt["id"])
    assert e.value.details["verb"] == "delete_document" and e.value.details["id"] == receipt["id"]
    assert faked.documents.delete(receipt["id"], confirm=True) == {"id": receipt["id"], "status": "deleted"}
    assert faked.memories.list() == EMPTY_LIST
    assert faked.documents.list() == EMPTY_LIST


def test_a_document_round_trip(faked):
    added = faked.add("Design notes: the ledger runs on Postgres", title="Design", custom_id="design-1")
    assert added["kind"] == "file" and added["status"] == "succeeded"
    again = faked.documents.add("Design notes: the ledger runs on Postgres", custom_id="design-1")
    assert again["id"] == added["id"] and again["status"] == "succeeded"
    [doc] = faked.documents.list()["items"]
    assert doc == {**faked.documents.get(added["id"])}
    assert doc["kind"] == "file" and doc["custom_id"] == "design-1" and doc["memory_name"] == "default"
    [hit] = faked.search("postgres")["items"]
    assert hit["type"] == "document" and "id" not in hit and hit["title"] == "Design"
    assert faked.documents.delete(added["id"], confirm=True) == {"id": added["id"], "status": "deleted"}
    assert faked.documents.list() == EMPTY_LIST


def test_lists_take_a_limit_and_say_what_was_left_out(faked):
    for fact in ("one", "two", "three"):
        faked.memories.add(fact)
    out = faked.memories.list(limit=2)
    assert [m["content"] for m in out["items"]] == ["three", "two"] and out["has_more"] is True
    assert faked.documents.list(limit=1)["has_more"] is True
    with pytest.raises(BadRequestError):
        faked.memories.list(limit=0)
    with pytest.raises(BadRequestError):
        faked.documents.list(limit=1001)


def test_writes_land_in_the_write_target(faked, monkeypatch):
    backend = faked._http._transport.backend
    backend._create("Work")
    backend._create("Life")
    with pytest.raises(WriteTargetRequiredError) as e:
        faked.memories.add("We picked Postgres")
    assert e.value.status == 422 and e.value.details == {"memories_in_reach": 2}
    with pytest.raises(WriteTargetRequiredError):
        faked.add("notes")
    assert faked.documents.list() == EMPTY_LIST

    monkeypatch.setenv("MEMBASE_WRITE_TARGET", "Work")
    receipt = faked.memories.add("We picked Postgres")
    assert receipt["learning"] == [{"memory_name": "Work", "status": "succeeded"}]
    assert faked.search("postgres")["items"][0]["memory_name"] == "Work"
    assert {m["name"] for m in faked.capabilities()["memories"]} == {"Work", "Life"}


def test_refusals_answer_in_the_error_envelope(local):
    with pytest.raises(BadRequestError) as e:
        local.search("")
    assert e.value.code == "validation"
    with pytest.raises(UnprocessableEntityError) as e:
        local.search("x", limit=99)
    assert e.value.code == "validation"
    with pytest.raises(UnprocessableEntityError):
        local.search("x", mode="slow")
    with pytest.raises(BadRequestError):
        local.add(None)
    for wrong in ("doc_missing", "mem_" + "x" * 8, ids.encode("document", "default:1"), "default:999"):
        with pytest.raises(NotFoundError) as e:
            local.memories.get(wrong)
        assert e.value.details == {"id": wrong}
    with pytest.raises(NotFoundError):
        local.documents.get(ids.encode("memory", "default:1"))
    with pytest.raises(NotFoundError):
        local.memories.forget(ids.encode("memory", "default:999"), confirm=True)


@pytest.mark.parametrize("path, body", [
    ("/v1/search", {"q": "x", "container": "default"}),
    ("/v1/search", {"q": "x", "threshold": 0.5}),
    ("/v1/memories/add", {"content": "x", "container": "default"}),
    ("/v1/memories/add", {"content": "x", "static": True}),
    ("/v1/documents/add", {"content": "x", "container": "default"}),
    ("/v1/documents/add", {"content": "x", "metadata": {}}),
    ("/v1/media/search", {"q": "x", "container": "default"}),
])
def test_unknown_fields_are_refused(local, path, body):
    r = local._http.post(path, json=body)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation"
    assert [f["loc"][-1] for f in err["details"]["fields"]] == [k for k in body if k not in ("q", "content")]


def test_the_retired_routes_are_gone(local):
    assert local._http.get("/v1/containers").status_code == 404
    # writes are POST + an action; the old method on the old path is not allowed
    assert local._http.post("/v1/memories", json={"content": "x"}).status_code == 405
    assert local._http.post("/v1/documents", json={"content": "x"}).status_code == 405
    for path in ("/v1/memories/mem_x", "/v1/documents/doc_x", "/v1/media/med_x"):
        r = local._http.delete(path, params={"confirm": "true"})
        assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed", path


def test_the_http_server_answers_the_same_routes(tmp_path):
    httpd = make_server(port=0, root=str(tmp_path / "srv"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        remote = Membase("local", base_url=base, max_retries=0)
        assert remote.memories.list() == EMPTY_LIST
        assert httpx.get(f"{base}/v1/nope").status_code == 404
        missing = ids.encode("memory", "default:999")
        r = httpx.post(f"{base}/v1/memories/{missing}/forget", params={"confirm": "true"})
        assert r.status_code == 404 and r.json()["error"]["details"] == {"id": missing}
        r = httpx.delete(f"{base}/v1/memories/{missing}", params={"confirm": "true"})
        assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"
        r = httpx.post(f"{base}/v1/search", content=b"{not json", headers={"Content-Type": "application/json"})
        assert r.status_code == 422
        remote.close()
    finally:
        httpd.shutdown()
        httpd.backend.close()


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs a model provider key")
def test_round_trip_through_the_engine(local):
    receipt = local.memories.add(
        "User: We picked Postgres for the ledger service.\nAssistant: Noted, Postgres for the ledger.",
    )
    assert receipt["status"] == "succeeded" and receipt["kind"] == "note", receipt
    hits = local.search("what database did we pick for the ledger?")
    assert hits["items"] and "Postgres" in hits["items"][0]["content"]
    assert hits["items"][0]["memory_name"] == "default"
    answer = local.ask("Which database is the ledger on?")
    assert "postgres" in answer["answer"].lower()
    fact = next(i for i in hits["items"] if i["type"] == "memory")
    with pytest.raises(ConfirmationRequiredError):
        local.memories.forget(fact["id"])
    assert local.memories.forget(fact["id"], confirm=True) == {"id": fact["id"], "status": "forgotten"}
