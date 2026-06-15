from typing import Optional, Callable
import requests
import json
import os
import time
from io import BytesIO
from urllib.parse import urlencode

from membase.storage.backend import HubBackend
from membase.storage.reliable_queue import (
    PersistentUploadQueue, STATUS_DONE, STATUS_DEAD,
)

import logging
logger = logging.getLogger(__name__)

# P0-1:连接/读取 timeout(秒)。可被 env MEMBASE_HUB_TIMEOUT 覆盖(单值=读超时)。
def _default_timeout():
    t = os.getenv("MEMBASE_HUB_TIMEOUT")
    return (5.0, float(t)) if t else (5.0, 30.0)


class Client(HubBackend):
    """Legacy 中心化 hub 客户端,带 P0 可靠性层(持久化队列 + 重试 + 幂等 + timeout)。

    上传不再静默丢数据:入队落盘 -> 后台 worker 带退避重试 -> 仍失败进死信(可观测)。
    读路径加 timeout,失败返回 None(保持向后兼容)。
    """

    def __init__(
        self,
        base_url,
        *,
        poster: Optional[Callable] = None,
        db_path: Optional[str] = None,
        account: Optional[str] = None,
        start_worker: bool = True,
        poll_interval: float = 0.5,
        wait_timeout: float = 60.0,
        raise_on_error: bool = False,
        on_error: Optional[Callable[[dict], None]] = None,
        timeout=None,
        requester: Optional[Callable] = None,
        read_retries: int = 2,
        read_backoff: float = 0.3,
        **queue_kwargs,
    ):
        self.base_url = base_url
        self.membase_id = os.getenv('MEMBASE_ID', '')
        self.timeout = timeout or _default_timeout()
        self.wait_timeout = wait_timeout
        self.raise_on_error = raise_on_error
        self._requester = requester or requests.post  # 注入点(测试/代理)
        self.read_retries = read_retries
        self.read_backoff = read_backoff

        if db_path is None:
            acct = account or os.getenv('MEMBASE_ACCOUNT', 'default')
            db_path = os.path.join(os.path.expanduser('~'), '.membase', acct, 'upload_queue.db')

        self._queue = PersistentUploadQueue(
            db_path, poster or self._post_upload, on_dead=on_error, **queue_kwargs)
        if start_worker:
            # P0-6 启动即捞 pending/到期重试项 —— 对账=队列本身
            self._queue.start(poll_interval=poll_interval)

    # ----- 传输层(默认 poster):带 timeout + 响应校验(P0-1/P0-2) ----------- #

    def _post_upload(self, owner, bucket, msg_id, message):
        body = json.dumps({"owner": owner, "bucket": bucket, "id": msg_id, "message": message})
        resp = self._requester(f"{self.base_url}/api/upload",
                               headers={'Content-Type': 'application/json'},
                               data=body, timeout=self.timeout)
        resp.raise_for_status()  # 非 2xx 抛 -> 队列重试,不静默(此处不自retry,队列负责)
        logger.debug("Upload done: %s/%s", owner, msg_id)

    def _read_with_retry(self, label, **kwargs):
        """读路径请求 + 短退避重试(P0:瞬时错误自愈)。最终失败返回 None(向后兼容)。"""
        delay = self.read_backoff
        for attempt in range(self.read_retries + 1):
            try:
                resp = self._requester(timeout=self.timeout, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.RequestException as err:
                if attempt >= self.read_retries:
                    logger.error("%s failed after %d attempts: %s", label, attempt + 1, err)
                    return None
                time.sleep(delay)
                delay *= 2

    def initialize(self, base_url):
        if self.base_url is None:
            self.base_url = base_url

    def _resolve_bucket(self, owner, msg, bucket):
        if bucket is not None:
            return bucket
        default_bucket = self.membase_id or owner
        if isinstance(msg, str):
            try:
                return json.loads(msg).get("name", default_bucket)
            except json.JSONDecodeError:
                return default_bucket
        return default_bucket

    def upload_hub(self, owner, filename, msg, bucket: Optional[str] = None, wait=True):
        """入队上传(持久化 + 幂等 + 后台重试)。

        wait=True 阻塞至完成/死信(上限 wait_timeout,超时返回 pending,后台仍重试)。
        返回 status:completed / failed / pending / queued;入队异常返回 None。
        """
        try:
            bucket = self._resolve_bucket(owner, msg, bucket)
            message = msg if isinstance(msg, str) else json.dumps(msg)
            key = self._queue.enqueue(owner, bucket, filename, message)
        except Exception as e:
            logger.error("Error queueing upload task: %s", e)
            return None

        if not wait:
            return {"status": "queued"}

        status = self._queue.wait(key, timeout=self.wait_timeout)
        if status == STATUS_DONE:
            return {"status": "completed"}
        if status == STATUS_DEAD:
            if self.raise_on_error:
                raise RuntimeError(f"upload failed permanently: {owner}/{filename}")
            return {"status": "failed"}
        return {"status": "pending"}  # 超时未决,后台继续重试(数据已落盘不丢)

    def upload_hub_data(self, owner, filename, data):
        """Upload meme data to the hub server with multipart form。"""
        files = {'file': (filename, BytesIO(data), 'application/octet-stream')}
        resp = self._read_with_retry(
            "uploadData", url=f"{self.base_url}/api/uploadData",
            files=files, data={'owner': owner})
        return resp.json() if resp is not None else None

    def list_conversations(self, owner):
        """List all conversations for a given owner."""
        resp = self._read_with_retry(
            "list conversations", url=f"{self.base_url}/api/conversation",
            data=urlencode({'owner': owner}),
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        return resp.json() if resp is not None else None

    def get_conversation(self, owner, conversation_id):
        """Get a conversation for a given owner and conversation id."""
        resp = self._read_with_retry(
            "get conversation", url=f"{self.base_url}/api/conversation",
            data=urlencode({'owner': owner, 'id': conversation_id}),
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        return resp.json() if resp is not None else None

    def download_hub(self, owner, filename):
        """Download meme data from the hub server."""
        resp = self._read_with_retry(
            "download", url=f"{self.base_url}/api/download",
            data=urlencode({'id': filename, 'owner': owner}),
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        return resp.content if resp is not None else None

    def wait_for_upload_queue(self, timeout: float = 30.0):
        """阻塞直到无 pending(优雅退出)。"""
        return self._queue.join_drain(timeout)

    # ----- 可观测(P0-2) ---------------------------------------------------- #

    @property
    def upload_stats(self) -> dict:
        return self._queue.stats

    def upload_counts(self) -> dict:
        return self._queue.counts()

    def dead_letters(self) -> list:
        return self._queue.dead_letters()

def build_hub_client() -> HubBackend:
    """根据 env 选择存储后端(adapter boundary,见 storage/backend.py)。

    MEMBASE_HUB_BACKEND:
      - 'legacy'(默认)-> 现状中心化 hub(Client)
      - 'da'            -> DAHubBackend(P1 可验证存储,坐到 Unibase DA 之上)
    MEMBASE_HUB: hub endpoint(两后端共用)。
    """
    base = os.getenv('MEMBASE_HUB', 'https://testnet.hub.membase.io')
    backend = os.getenv('MEMBASE_HUB_BACKEND', 'legacy').lower()
    if backend == 'da':
        # 延迟导入:仅在选用时才加载 DA 后端及其依赖
        from membase.storage.da_backend import DAHubBackend
        logger.info("Using DAHubBackend (MEMBASE_HUB_BACKEND=da)")
        return DAHubBackend(base)
    return Client(base)


he = os.getenv('MEMBASE_HUB', 'https://testnet.hub.membase.io')
hub_client = build_hub_client()