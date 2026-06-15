"""P0 残项测试:hub 读路径重试 + sqlite_memory upload_status 启动对账。"""

import os
import sqlite3
import tempfile

import pytest
import requests

from membase.storage.hub import Client


# ----- hub 读路径重试 ------------------------------------------------------- #

class FakeResp:
    def __init__(self, payload=None, content=b"", ok=True):
        self._payload, self.content, self._ok = payload, content, ok
    def raise_for_status(self):
        if not self._ok:
            raise requests.HTTPError("500")
    def json(self):
        return self._payload


class FlakyRequester:
    def __init__(self, fail_times, resp):
        self.calls, self.fail_times, self.resp = 0, fail_times, resp
    def __call__(self, *a, **k):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise requests.ConnectionError("transient")
        return self.resp


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _client(tmp, requester, **kw):
    return Client("http://hub", db_path=os.path.join(tmp, "c.db"),
                  start_worker=False, requester=requester, read_backoff=0, **kw)


def test_read_retries_then_succeeds(tmp):
    req = FlakyRequester(fail_times=2, resp=FakeResp(payload=["m0", "m1"]))
    c = _client(tmp, req, read_retries=2)
    assert c.get_conversation("o", "conv") == ["m0", "m1"]
    assert req.calls == 3                       # 2 失败 + 1 成功


def test_read_gives_up_after_retries(tmp):
    req = FlakyRequester(fail_times=99, resp=FakeResp())
    c = _client(tmp, req, read_retries=2)
    assert c.get_conversation("o", "conv") is None   # 最终失败返回 None(向后兼容)
    assert req.calls == 3                              # 1 + 2 retries


def test_download_and_list_use_retry(tmp):
    req = FlakyRequester(fail_times=1, resp=FakeResp(payload=["a"], content=b"bytes"))
    c = _client(tmp, req, read_retries=2)
    assert c.download_hub("o", "id") == b"bytes"
    req2 = FlakyRequester(fail_times=1, resp=FakeResp(payload=["a"]))
    c2 = _client(tmp, req2, read_retries=2)
    assert c2.list_conversations("o") == ["a"]


# ----- sqlite_memory 启动对账 ----------------------------------------------- #

class FakeHub:
    def __init__(self): self.calls = []
    def upload_hub(self, owner, msg_id, content, bucket=None, wait=True):
        self.calls.append((owner, msg_id, content))
        return {"status": "queued"}


def _make_db(path, rows):
    """rows: (id, conversation_id, content, memory_index, upload_status, memory_type)"""
    conn = sqlite3.connect(path)
    conn.execute('''CREATE TABLE memories (
        id TEXT PRIMARY KEY, conversation_id TEXT, content TEXT, memory_index INTEGER,
        upload_status INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        memory_type TEXT)''')
    conn.executemany(
        "INSERT INTO memories (id, conversation_id, content, memory_index, upload_status, memory_type) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.commit(); conn.close()


def _sqlite_mem(db_path):
    from membase.memory.sqlite_memory import SqliteMemory
    obj = SqliteMemory.__new__(SqliteMemory)   # 绕过 __init__(避开 Chroma/线程)
    obj.db_path = db_path
    obj.membase_account = "acct"
    obj.auto_upload_to_hub = True
    return obj


def test_reconcile_reenqueues_only_pending(tmp, monkeypatch):
    db = os.path.join(tmp, "sql.db")
    _make_db(db, [
        ("id0", "convA", '{"name":"convA","content":"x0"}', 0, 0, "stm"),  # pending
        ("id1", "convA", '{"name":"convA","content":"x1"}', 1, 1, "stm"),  # already done
        ("id2", "convB", '{"name":"convB","content":"y0"}', 0, 0, "stm"),  # pending
    ])
    fake = FakeHub()
    import membase.memory.sqlite_memory as sm
    monkeypatch.setattr(sm, "hub_client", fake)

    n = _sqlite_mem(db).reconcile_uploads()
    assert n == 2
    # 只重排 status=0 的两条,msg_id = conv_{index},content = stored 列
    sent = {mid: content for _, mid, content in fake.calls}
    assert set(sent) == {"convA_0", "convB_0"}
    assert sent["convA_0"] == '{"name":"convA","content":"x0"}'
    # 全部置为已入队
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE upload_status=0").fetchone()[0] == 0
    conn.close()


def test_reconcile_noop_when_all_done(tmp, monkeypatch):
    db = os.path.join(tmp, "sql.db")
    _make_db(db, [("id1", "c", '{"name":"c"}', 0, 1, "stm")])
    fake = FakeHub()
    import membase.memory.sqlite_memory as sm
    monkeypatch.setattr(sm, "hub_client", fake)
    assert _sqlite_mem(db).reconcile_uploads() == 0
    assert fake.calls == []
