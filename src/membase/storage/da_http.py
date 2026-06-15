"""DA hub HTTP 客户端 seam(P1):HotTier / ColdTier 打到 Unibase DA hub。

DA hub 的 /api/upload(owner,bucket,id,message)+ /api/download(owner,id)与 membase hub
**几乎同构**(da-sdk-go/hub/upload.go 的 logFSWrite 按 owner/bucket/key 存)。故这两层
直接映射到既有 DA hub 端点:
  HotTier.push       -> POST /api/upload   {owner, bucket=segment_id, id=f"{seg}_{idx}", message=b64(cipher)}
  HotTier.read_chunk -> POST /api/download {owner, id=f"{seg}_{idx}"} -> b64 -> bytes
  ColdTier.read      -> POST /api/download {owner, id=da_cid} 取整段密文,本地按 [offset:len] 切

requester 可注入(默认 requests.post),便于无 hub 的契约测试。

⚠️ 未验证项:本环境无真 DA hub,以上为对 DA hub API 的忠实映射但未端到端跑通;
   SealService(封段 = Go 侧 sdk.Upload 产 KZG CID + pn_solidity + cost)需 DA hub
   **新增** /api/seal 端点,不在本模块内造(避免臆造 Go API)。
"""

from __future__ import annotations

import base64
import json as _json
from typing import Callable, Optional
from urllib.parse import urlencode

import requests

import logging
logger = logging.getLogger(__name__)


def _hot_key(segment_id: str, chunk_idx: int) -> str:
    return f"{segment_id}_{chunk_idx}"


class HttpHotTier:
    """DA hub logfs 热层:每 chunk 一条 key(b64 密文)。满足 segment.HotTier。"""

    def __init__(self, base_url: str, *, requester: Optional[Callable] = None,
                 timeout=(5.0, 30.0)):
        self.base_url = base_url.rstrip("/")
        self._post = requester or requests.post
        self.timeout = timeout

    def push(self, *, owner, segment_id, chunk_idx, cipher_bytes):
        body = _json.dumps({
            "owner": owner, "bucket": segment_id, "id": _hot_key(segment_id, chunk_idx),
            "message": base64.b64encode(cipher_bytes).decode("ascii"),
        })
        resp = self._post(f"{self.base_url}/api/upload",
                          headers={"Content-Type": "application/json"},
                          data=body, timeout=self.timeout)
        resp.raise_for_status()

    def read_chunk(self, *, owner, segment_id, chunk_idx):
        resp = self._post(
            f"{self.base_url}/api/download",
            data=urlencode({"owner": owner, "id": _hot_key(segment_id, chunk_idx)}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout)
        resp.raise_for_status()
        return base64.b64decode(resp.content)


class HttpColdTier:
    """DA 冷层读:按 da_cid 取整段密文(纠删重建后的 blob),本地切片。满足 segment.ColdTier。

    单段下载缓存,避免同段多条消息重复下载(§7.1:日常读不做 CheckFileFull)。
    """

    def __init__(self, base_url: str, owner: str, *, requester: Optional[Callable] = None,
                 timeout=(5.0, 60.0)):
        self.base_url = base_url.rstrip("/")
        self.owner = owner
        self._post = requester or requests.post
        self.timeout = timeout
        self._cache: dict = {}  # da_cid -> 整段密文 bytes

    def _download(self, da_cid: str) -> bytes:
        if da_cid in self._cache:
            return self._cache[da_cid]
        resp = self._post(
            f"{self.base_url}/api/download",
            data=urlencode({"owner": self.owner, "id": da_cid}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout)
        resp.raise_for_status()
        blob = resp.content
        self._cache[da_cid] = blob
        return blob

    def read(self, *, da_cid, cipher_offset, cipher_len):
        blob = self._download(da_cid)
        return blob[cipher_offset: cipher_offset + cipher_len]
