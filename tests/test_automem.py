"""Auto-memory store: CRUD against an in-memory key-value store, and, opt-in, against the real Membase Protocol hub."""

from __future__ import annotations

import os
import time
import uuid

import pytest

from membase.mcp.automem import AutoMemoryStore

_RUN_HUB = os.environ.get("MEMBASE_MCP_TEST_HUB") == "1"


TESTNET_HUB = "https://testnet.hub.membase.com"


class MemoryLog:
    def __init__(self) -> None:
        self.data: list[bytes] = []

    def append(self, entry: bytes) -> None:
        self.data.append(entry)

    def entries(self) -> list[bytes]:
        return list(self.data)


@pytest.fixture(scope="module", params=["memory", "hub"])
def store(request):
    if request.param == "memory":
        return AutoMemoryStore(MemoryLog())
    if not _RUN_HUB:
        pytest.skip("Set MEMBASE_MCP_TEST_HUB=1 to run against the real hub.")
    pytest.importorskip("membase_protocol", reason="needs membase-protocol")
    from membase_protocol.core.persistence.wallet import Wallet

    from membase.mcp.automem import ProtocolLog

    wallet = Wallet.generate()
    hub_url = os.environ.get("MEMBASE_HUB") or TESTNET_HUB
    return AutoMemoryStore(ProtocolLog.from_private_key(wallet.private_key_hex, hub_url))


@pytest.fixture
def scope():
    """Each test gets a fresh scope so parallel/sequential runs don't share state."""
    return f"test-{uuid.uuid4().hex[:12]}"


def _wait_for(predicate, *, timeout: float = 30.0, interval: float = 0.5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def _wait_for_state(store, scope, mem_id, *, present: bool, timeout: float = 30.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = store.fetch(scope, mem_id)
        if present and last is not None:
            return last
        if not present and last is None:
            return None
        time.sleep(0.5)
    raise AssertionError(
        f"timeout waiting for fetch(scope={scope}, id={mem_id}) "
        f"present={present}; last={last!r}"
    )


def test_save_then_fetch_roundtrip(store, scope):
    saved = store.save(
        scope,
        type="feedback",
        name="prefer-real-db",
        description="integration tests must hit a real database",
        content="Lead: real DB.\n\n**Why:** prior incident.\n**How to apply:** integration suites only.",
        source_session_id="test-session-001",
    )
    assert saved.id
    assert saved.scope == scope
    assert saved.type == "feedback"
    assert saved.created_at == saved.updated_at

    got = _wait_for_state(store, scope, saved.id, present=True)
    assert got.id == saved.id
    assert got.name == "prefer-real-db"
    assert got.description == "integration tests must hit a real database"
    assert got.content.startswith("Lead: real DB.")
    assert got.source_session_id == "test-session-001"
    assert got.client == "claude-code"


def test_save_same_name_updates_in_place(store, scope):
    first = store.save(
        scope,
        type="user",
        name="role",
        description="data scientist",
        content="user is a data scientist",
    )
    _wait_for_state(store, scope, first.id, present=True)

    second = store.save(
        scope,
        type="user",
        name="role",
        description="data scientist working on observability",
        content="user is a data scientist focused on observability",
    )
    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at

    got = _wait_for(
        lambda: (
            store.fetch(scope, first.id)
            if (m := store.fetch(scope, first.id)) and "observability" in m.description
            else None
        )
    )
    assert got is not None
    assert "observability" in got.description
    assert "observability" in got.content


def test_list_returns_only_live_records(store, scope):
    a = store.save(scope, type="reference", name="a-doc", description="a", content="aaa")
    b = store.save(scope, type="reference", name="b-doc", description="b", content="bbb")
    c = store.save(scope, type="reference", name="c-doc", description="c", content="ccc")

    _wait_for_state(store, scope, a.id, present=True)
    _wait_for_state(store, scope, b.id, present=True)
    _wait_for_state(store, scope, c.id, present=True)

    store.delete(scope, b.id)
    _wait_for_state(store, scope, b.id, present=False)

    live = store.list(scope)
    live_ids = {m.id for m in live}
    assert a.id in live_ids
    assert c.id in live_ids
    assert b.id not in live_ids


def test_list_scopes_includes_our_scope(store, scope):
    saved = store.save(
        scope, type="project", name="hello", description="hi", content="hello world"
    )
    _wait_for_state(store, scope, saved.id, present=True)

    scopes = store.list_scopes()
    assert scope in scopes


def test_delete_then_resave_revives_record(store, scope):
    saved = store.save(
        scope,
        type="feedback",
        name="ephemeral",
        description="will be deleted",
        content="content v1",
    )
    _wait_for_state(store, scope, saved.id, present=True)

    store.delete(scope, saved.id)
    _wait_for_state(store, scope, saved.id, present=False)

    revived = store.save(
        scope,
        type="feedback",
        name="ephemeral",
        description="brought back",
        content="content v2",
    )
    assert revived.id == saved.id

    got = _wait_for(
        lambda: (
            m if (m := store.fetch(scope, saved.id)) and m.description == "brought back" else None
        )
    )
    assert got is not None
    assert got.description == "brought back"
    assert got.content == "content v2"


def test_stable_id_is_deterministic():
    """Pure-function check; no network."""
    from membase.mcp.automem import _stable_id_for

    a = _stable_id_for("user", "role")
    b = _stable_id_for("user", "role")
    c = _stable_id_for("project:foo", "role")
    assert a == b
    assert a != c
    assert len(a) == 16
