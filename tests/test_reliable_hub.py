"""P0 hub 可靠性测试:持久化队列(崩溃安全/退避重试/幂等/死信)+ Client 集成。

队列测试用注入 clock/poster + start_worker=False,完全确定性、无网络无线程。
"""

import os
import tempfile

import pytest

from membase.storage.reliable_queue import (
    PersistentUploadQueue, STATUS_DONE, STATUS_DEAD, STATUS_PENDING,
)
from membase.storage.hub import Client


class Clock:
    def __init__(self, t=0.0): self.t = t
    def __call__(self): return self.t
    def advance(self, d): self.t += d


def _q(tmp, poster, clock=None, **kw):
    clock = clock or Clock()
    q = PersistentUploadQueue(os.path.join(tmp, "q.db"), poster,
                              clock=clock, rng=lambda: 0.5,  # jitter 中点 -> 确定
                              **kw)
    return q, clock


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory() as d:
        yield d


# ----- 队列核心 ------------------------------------------------------------- #

def test_success_path(tmp):
    calls = []
    q, _ = _q(tmp, lambda *a: calls.append(a))
    q.enqueue("o", "b", "m1", "msg")
    assert q.process_once() == 1
    assert q.counts() == {STATUS_DONE: 1}
    assert q.stats["done"] == 1 and len(calls) == 1


def test_idempotent_no_double_upload(tmp):
    calls = []
    q, _ = _q(tmp, lambda *a: calls.append(a))
    q.enqueue("o", "b", "m1", "msg")
    q.enqueue("o", "b", "m1", "msg")          # 重复入队 -> 同一行,不重复
    assert q.pending_count() == 1
    q.process_once()
    q.enqueue("o", "b", "m1", "msg")          # 已 done -> 跳过
    q.process_once()
    assert len(calls) == 1                     # 只真正上传一次
    assert q.counts() == {STATUS_DONE: 1}


def test_retry_with_backoff_then_success(tmp):
    n = {"i": 0}
    def poster(*a):
        n["i"] += 1
        if n["i"] < 3:
            raise RuntimeError("boom")
    q, clk = _q(tmp, poster, max_attempts=5, base_backoff=10,
                backoff_factor=2, jitter=0)
    q.enqueue("o", "b", "m1", "msg")
    assert q.process_once() == 1               # 尝试1 失败 -> next=10
    assert q.process_once() == 0               # 未到期
    assert q.pending_count() == 1
    clk.advance(10); assert q.process_once() == 1   # 尝试2 失败 -> next=10+20=30
    clk.advance(20); assert q.process_once() == 1   # 尝试3 成功
    assert n["i"] == 3 and q.counts() == {STATUS_DONE: 1}


def test_dead_letter_no_silent_loss(tmp):
    dead = []
    q, clk = _q(tmp, lambda *a: (_ for _ in ()).throw(RuntimeError("down")),
                max_attempts=2, base_backoff=1, jitter=0,
                on_dead=lambda row: dead.append(row))
    q.enqueue("o", "b", "m1", "msg")
    q.process_once()           # 尝试1 失败
    clk.advance(1)
    q.process_once()           # 尝试2 -> 死信
    assert q.counts() == {STATUS_DEAD: 1}
    assert q.stats["dead"] == 1
    assert len(dead) == 1 and dead[0]["msg_id"] == "m1"        # 失败可观测
    dl = q.dead_letters()
    assert len(dl) == 1 and "down" in dl[0]["error"]


def test_crash_safety_and_reconciliation(tmp):
    # q1:失败 -> pending 落盘;"重启" q2 同库继续到成功
    clk = Clock()
    db = os.path.join(tmp, "q.db")
    q1 = PersistentUploadQueue(db, lambda *a: (_ for _ in ()).throw(RuntimeError("x")),
                               clock=clk, jitter=0, base_backoff=5)
    q1.enqueue("o", "b", "m1", "msg")
    q1.process_once()                          # 失败 -> pending(next=5)
    assert q1.pending_count() == 1

    # 新进程:同一 db,pending 仍在(对账=队列本身),换成功 poster
    calls = []
    q2 = PersistentUploadQueue(db, lambda *a: calls.append(a), clock=clk, jitter=0)
    assert q2.pending_count() == 1             # 崩溃前的 pending 恢复
    clk.advance(5)
    q2.process_once()
    assert len(calls) == 1 and q2.counts() == {STATUS_DONE: 1}


def test_wait_returns_terminal_status(tmp):
    q, _ = _q(tmp, lambda *a: None)
    k = q.enqueue("o", "b", "m1", "msg")
    q.process_once()
    assert q.wait(k, timeout=0.1) == STATUS_DONE


# ----- Client 集成 --------------------------------------------------------- #

def test_client_success_roundtrip(tmp):
    calls = []
    c = Client("http://hub", poster=lambda *a: calls.append(a),
               db_path=os.path.join(tmp, "c.db"), poll_interval=0.01)
    r = c.upload_hub("o", "m1", '{"name":"conv"}', wait=True)
    assert r["status"] == "completed"
    assert len(calls) == 1
    # bucket 由 msg.name 推出
    assert calls[0][1] == "conv"


def test_client_dead_letter_failed_status(tmp):
    c = Client("http://hub", poster=lambda *a: (_ for _ in ()).throw(RuntimeError("no")),
               db_path=os.path.join(tmp, "c.db"), poll_interval=0.01,
               max_attempts=1, base_backoff=0, jitter=0)
    r = c.upload_hub("o", "m1", "msg", wait=True)
    assert r["status"] == "failed"
    assert len(c.dead_letters()) == 1


def test_client_raise_on_error(tmp):
    c = Client("http://hub", poster=lambda *a: (_ for _ in ()).throw(RuntimeError("no")),
               db_path=os.path.join(tmp, "c.db"), poll_interval=0.01,
               max_attempts=1, base_backoff=0, jitter=0, raise_on_error=True)
    with pytest.raises(RuntimeError):
        c.upload_hub("o", "m1", "msg", wait=True)


def test_client_no_wait_queues(tmp):
    c = Client("http://hub", poster=lambda *a: None,
               db_path=os.path.join(tmp, "c.db"), start_worker=False)
    r = c.upload_hub("o", "m1", "msg", wait=False)
    assert r["status"] == "queued"
    assert c.upload_counts().get(STATUS_PENDING) == 1   # 已落盘
