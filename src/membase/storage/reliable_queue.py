"""PersistentUploadQueue — hub 上传的持久化可靠队列(P0 信任地基)。

解决 legacy hub 的静默丢数据(上传失败只 log 不 raise、队列在内存、无 timeout/
重试/幂等)。一个 SQLite 表即同时给到:

  P0-3 崩溃安全   pending 落盘,进程崩溃重启后仍在
  P0-4 退避重试   失败按指数退避 + jitter 重排,超过上限进死信(dead)
  P0-5 幂等       key=(owner,bucket,id);已 done 不重传,pending 不重复入队
  P0-6 启动对账   重启即从表里捞 pending/到期重试项继续 —— 对账=队列本身

传输层(实际 POST)做成可注入 seam(poster),默认 requests+timeout,测试注入 fake。
"""

from __future__ import annotations

import os
import time
import json
import sqlite3
import hashlib
import threading
from typing import Optional, Callable

import logging
logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_DEAD = "dead"


def _key(owner: str, bucket: str, msg_id: str) -> str:
    raw = f"{owner}\x1f{bucket}\x1f{msg_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class PersistentUploadQueue:
    def __init__(
        self,
        db_path: str,
        poster: Callable[[str, str, str, str], None],
        *,
        max_attempts: int = 8,
        base_backoff: float = 1.0,
        backoff_factor: float = 2.0,
        max_backoff: float = 60.0,
        jitter: float = 0.2,
        clock: Callable[[], float] = time.time,
        rng: Optional[Callable[[], float]] = None,
        on_dead: Optional[Callable[[dict], None]] = None,
    ):
        """
        poster(owner, bucket, msg_id, message) -> None,失败抛异常。
        on_dead(row_dict):某条彻底失败(进死信)时回调 —— P0-2 失败可观测。
        """
        self.db_path = db_path
        self._poster = poster
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self.backoff_factor = backoff_factor
        self.max_backoff = max_backoff
        self.jitter = jitter
        self._clock = clock
        self._rng = rng  # 返回 [0,1);为 None 时用 random(测试可注入确定值)
        self._on_dead = on_dead

        self._lock = threading.RLock()
        self._events: dict = {}  # key -> threading.Event(wait=True 用)
        self._stats = {"enqueued": 0, "done": 0, "failed_attempts": 0, "dead": 0}

        if os.path.dirname(db_path):
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS upload_queue (
                key TEXT PRIMARY KEY,
                owner TEXT, bucket TEXT, msg_id TEXT, message TEXT,
                status TEXT, attempts INTEGER,
                next_attempt REAL, created_at REAL, updated_at REAL,
                last_error TEXT, kind TEXT DEFAULT ''
            )""")
        # migrate pre-existing DBs (kind column added later); ignore if present
        try:
            self._conn.execute("ALTER TABLE upload_queue ADD COLUMN kind TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

        self._running = False
        self._worker: Optional[threading.Thread] = None

    # ----- 入队(幂等) ------------------------------------------------------ #

    def enqueue(self, owner: str, bucket: str, msg_id: str, message: str, kind: str = "") -> str:
        k = _key(owner, bucket, msg_id)
        now = self._clock()
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM upload_queue WHERE key=?", (k,)).fetchone()
            if row is not None and row[0] == STATUS_DONE:
                logger.debug("idempotent skip (already done): %s", msg_id)
                self._signal(k)  # 已完成,wait 立即返回
                return k
            # 新建或复活(dead/pending 重新入队):重置为 pending,立即可投
            # kind 是 bucket 场景(memory 默认 / knowledgebase / …),随 item 持久化,
            # 崩溃/重启后 replay 仍带得上;hub 侧只在首次建 bucket 时用它。
            self._conn.execute("""
                INSERT INTO upload_queue
                    (key, owner, bucket, msg_id, message, status, attempts,
                     next_attempt, created_at, updated_at, last_error, kind)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    status=excluded.status, next_attempt=excluded.next_attempt,
                    message=excluded.message, updated_at=excluded.updated_at,
                    kind=excluded.kind
            """, (k, owner, bucket, msg_id, message, STATUS_PENDING, 0,
                  now, now, now, None, kind))
            self._conn.commit()
            self._stats["enqueued"] += 1
            self._events.pop(k, None)  # 清掉旧 event,等待新一轮
        return k

    # ----- 处理 -------------------------------------------------------------- #

    def process_once(self) -> int:
        """处理当前所有到期 pending 项一遍,返回处理条数。worker 与测试共用。"""
        now = self._clock()
        with self._lock:
            rows = self._conn.execute("""
                SELECT key, owner, bucket, msg_id, message, attempts, kind
                FROM upload_queue WHERE status=? AND next_attempt<=?
                ORDER BY next_attempt
            """, (STATUS_PENDING, now)).fetchall()
        for k, owner, bucket, msg_id, message, attempts, kind in rows:
            self._attempt(k, owner, bucket, msg_id, message, attempts, kind)
        return len(rows)

    def _attempt(self, k, owner, bucket, msg_id, message, attempts, kind=""):
        try:
            # Pass kind only when set, so injected posters with the historical
            # (owner, bucket, msg_id, message) signature keep working — only
            # kind-aware uploads (e.g. knowledgebase) opt into the extra arg.
            if kind:
                self._poster(owner, bucket, msg_id, message, kind=kind)
            else:
                self._poster(owner, bucket, msg_id, message)
        except Exception as e:  # 传输失败:退避重排,或进死信
            attempts += 1
            with self._lock:
                self._stats["failed_attempts"] += 1
                if attempts >= self.max_attempts:
                    self._conn.execute(
                        "UPDATE upload_queue SET status=?, attempts=?, updated_at=?, last_error=? WHERE key=?",
                        (STATUS_DEAD, attempts, self._clock(), str(e), k))
                    self._conn.commit()
                    self._stats["dead"] += 1
                    logger.error("upload DEAD after %d attempts: %s/%s/%s: %s",
                                 attempts, owner, bucket, msg_id, e)
                    dead_row = {"owner": owner, "bucket": bucket, "msg_id": msg_id,
                                "message": message, "attempts": attempts, "error": str(e)}
                    self._signal(k)  # 失败也唤醒 wait(返回 failed)
                    cb = self._on_dead
                else:
                    delay = self._backoff(attempts)
                    self._conn.execute(
                        "UPDATE upload_queue SET attempts=?, next_attempt=?, updated_at=?, last_error=? WHERE key=?",
                        (attempts, self._clock() + delay, self._clock(), str(e), k))
                    self._conn.commit()
                    logger.warning("upload retry %d/%d in %.1fs: %s/%s: %s",
                                   attempts, self.max_attempts, delay, owner, msg_id, e)
                    cb = None
            if cb:
                try:
                    cb(dead_row)
                except Exception:
                    logger.exception("on_dead callback raised")
            return
        # 成功
        with self._lock:
            self._conn.execute(
                "UPDATE upload_queue SET status=?, updated_at=? WHERE key=?",
                (STATUS_DONE, self._clock(), k))
            self._conn.commit()
            self._stats["done"] += 1
            self._signal(k)

    def _backoff(self, attempts: int) -> float:
        delay = min(self.base_backoff * (self.backoff_factor ** (attempts - 1)), self.max_backoff)
        if self.jitter:
            r = self._rng() if self._rng else __import__("random").random()
            delay *= (1.0 + self.jitter * (2 * r - 1))  # ±jitter
        return max(0.0, delay)

    # ----- 等待 / 观测 ------------------------------------------------------- #

    def wait(self, key: str, timeout: Optional[float] = None) -> str:
        """阻塞至该 key 完成/死信。返回最终 status(done/dead/pending=超时未决)。"""
        with self._lock:
            st = self._status(key)
            if st in (STATUS_DONE, STATUS_DEAD):
                return st
            ev = self._events.get(key)
            if ev is None:
                ev = threading.Event()
                self._events[key] = ev
        ev.wait(timeout)
        return self._status(key)

    def _status(self, key: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM upload_queue WHERE key=?", (key,)).fetchone()
        return row[0] if row else STATUS_PENDING

    def _signal(self, key: str):
        ev = self._events.get(key)
        if ev is not None:
            ev.set()

    @property
    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def counts(self) -> dict:
        """各 status 当前条数(对账/监控)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM upload_queue GROUP BY status").fetchall()
        return {s: c for s, c in rows}

    def dead_letters(self) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT owner, bucket, msg_id, attempts, last_error FROM upload_queue WHERE status=?",
                (STATUS_DEAD,)).fetchall()
        return [{"owner": o, "bucket": b, "msg_id": m, "attempts": a, "error": e}
                for o, b, m, a, e in rows]

    def pending_count(self) -> int:
        return self.counts().get(STATUS_PENDING, 0)

    # ----- worker 线程 ------------------------------------------------------- #

    def start(self, poll_interval: float = 0.5):
        if self._running:
            return
        self._running = True

        def _loop():
            while self._running:
                try:
                    n = self.process_once()
                except Exception:
                    logger.exception("upload worker pass failed")
                    n = 0
                time.sleep(0 if n else poll_interval)

        self._worker = threading.Thread(target=_loop, daemon=True)
        self._worker.start()

    def stop(self):
        self._running = False

    def join_drain(self, timeout: float = 30.0):
        """阻塞直到无 pending(或超时)。优雅退出/测试用。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.pending_count() == 0:
                return True
            time.sleep(0.05)
        return False
