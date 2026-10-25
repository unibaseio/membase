from __future__ import annotations

import logging

import pytest
from langchain_core.runnables import RunnableLambda
from membase.errors import PermissionDeniedError

from langchain_membase import MembaseRetriever, MembaseUnavailableError, MissingUserIdError

from .conftest import FakeMembase


def test_invoke_returns_documents_citing_their_container(fake: FakeMembase) -> None:
    docs = MembaseRetriever(client=fake.client(), k=3).invoke("the ledger")
    assert len(docs) == 3
    assert {d.metadata["container_name"] for d in docs} == {"Project decisions", "Reading notes"}
    assert docs[0].metadata["container"] == "mv-decisions"
    assert "content" not in docs[0].metadata
    assert fake.requests[-1]["body"] == {"q": "the ledger", "limit": 3}


def test_container_and_k_at_call_time(fake: FakeMembase) -> None:
    retriever = MembaseRetriever(client=fake.client())
    docs = retriever.invoke("the ledger", k=2, container="mv-reading")
    assert len(docs) == 2
    assert fake.requests[-1]["body"] == {"q": "the ledger", "container": "mv-reading", "limit": 2}


def test_each_user_is_served_by_their_own_key(fake: FakeMembase) -> None:
    keys = {"alice": "mbk_alice", "bob": "mbk_bob"}
    retriever = MembaseRetriever(client_for=lambda uid: fake.client(keys[uid]), k=1)

    a = retriever.invoke("q", config={"configurable": {"user_id": "alice"}})
    b = retriever.batch(["q"], config={"configurable": {"user_id": "bob"}})[0]

    assert "mbk_alice" in a[0].page_content and "mbk_bob" in b[0].page_content
    assert [r["key"] for r in fake.requests] == ["mbk_alice", "mbk_bob"]


def test_user_id_flows_from_a_parent_chain(fake: FakeMembase) -> None:
    retriever = MembaseRetriever(client_for=lambda uid: fake.client(f"mbk_{uid}"), k=1)
    chain = RunnableLambda(lambda q: q.upper()) | retriever
    chain.invoke("q", config={"configurable": {"user_id": "carol"}})
    assert fake.requests[-1]["key"] == "mbk_carol"


async def test_ainvoke_with_user(fake: FakeMembase) -> None:
    retriever = MembaseRetriever(client_for=lambda uid: fake.client(f"mbk_{uid}"), k=2)
    docs = await retriever.ainvoke("q", config={"configurable": {"user_id": "dave"}})
    assert len(docs) == 2 and fake.requests[-1]["key"] == "mbk_dave"


def test_client_for_without_a_user_id_never_falls_back(fake: FakeMembase) -> None:
    retriever = MembaseRetriever(client_for=lambda uid: fake.client(f"mbk_{uid}"))
    with pytest.raises(MissingUserIdError):
        retriever.invoke("q")
    assert fake.requests == []


def test_client_for_and_client_are_exclusive(fake: FakeMembase) -> None:
    with pytest.raises(ValueError, match="not both"):
        MembaseRetriever(client=fake.client(), client_for=lambda uid: fake.client())


def test_revoked_key_is_a_clear_403(fake: FakeMembase) -> None:
    fake.revoked_keys.add("mbk_alice")
    with pytest.raises(PermissionDeniedError, match="narrowed or revoked"):
        MembaseRetriever(client=fake.client()).invoke("q")


def test_unavailable_memories_are_not_no_memory(fake: FakeMembase) -> None:
    fake.container_errors = {"mv-decisions": "no model", "mv-reading": "dormant"}
    with pytest.raises(MembaseUnavailableError, match="Project decisions: no model"):
        MembaseRetriever(client=fake.client()).invoke("q")


def test_partial_answer_is_returned_and_logged(fake: FakeMembase, caplog) -> None:
    fake.container_errors = {"mv-reading": "dormant"}
    with caplog.at_level(logging.WARNING, logger="langchain_membase"):
        docs = MembaseRetriever(client=fake.client(), k=2).invoke("q")
    assert len(docs) == 2 and all(d.metadata["container_name"] == "Project decisions" for d in docs)
    assert "Reading notes: dormant" in caplog.text


def test_empty_answer_is_empty(fake: FakeMembase) -> None:
    fake.no_results = True
    assert MembaseRetriever(client=fake.client()).invoke("q") == []


def test_api_host_defaults_to_membase_com_whatever_the_sdk_defaults_to(monkeypatch):
    monkeypatch.delenv("MEMBASE_BASE_URL", raising=False)
    monkeypatch.delenv("MEMBASE_LOCAL", raising=False)
    assert MembaseRetriever(api_key="mbk_x").client.base_url == "https://api.app.membase.com"
    monkeypatch.setenv("MEMBASE_BASE_URL", "https://env.membase.test")
    assert MembaseRetriever(api_key="mbk_x").client.base_url == "https://env.membase.test"
    retriever = MembaseRetriever(api_key="mbk_x", base_url="https://arg.membase.test")
    assert retriever.client.base_url == "https://arg.membase.test"
