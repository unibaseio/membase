"""DA hub HTTP 客户端测试:HotTier/ColdTier 对 /api/upload + /api/download 的忠实映射。

用 fake 内存 hub transport(不起真 hub),验证请求契约 + 编解码 round-trip。
"""

import base64
import json
from urllib.parse import parse_qs

import pytest
import requests

from membase.storage.da_http import HttpHotTier, HttpColdTier
from membase.storage.segment import HotTier, ColdTier


class FakeResp:
    def __init__(self, content=b"", ok=True):
        self.content, self._ok = content, ok
    def raise_for_status(self):
        if not self._ok:
            raise requests.HTTPError("404")


class FakeHub:
    """模拟 DA hub logfs:/api/upload 存,/api/download 取。"""
    def __init__(self):
        self.store = {}        # (owner, id) -> message(str b64) | bytes(raw blob)
        self.uploads = []      # 记录 upload 请求体,供契约断言
    def __call__(self, url, *, data=None, headers=None, timeout=None):
        if url.endswith("/api/upload"):
            body = json.loads(data); self.uploads.append(body)
            self.store[(body["owner"], body["id"])] = body["message"]
            return FakeResp(content=b"ok")
        if url.endswith("/api/download"):
            form = parse_qs(data)
            val = self.store.get((form["owner"][0], form["id"][0]))
            if val is None:
                return FakeResp(ok=False)
            return FakeResp(content=val.encode() if isinstance(val, str) else val)
        return FakeResp(ok=False)


def test_protocols():
    hub = FakeHub()
    assert isinstance(HttpHotTier("http://h", requester=hub), HotTier)
    assert isinstance(HttpColdTier("http://h", "acct", requester=hub), ColdTier)


def test_hot_push_read_roundtrip():
    hub = FakeHub()
    hot = HttpHotTier("http://hub", requester=hub)
    hot.push(owner="acct", segment_id="seg1", chunk_idx=3, cipher_bytes=b"\x00\x01secret\xff")
    # 契约:bucket=segment_id, id=seg_idx, message=b64
    up = hub.uploads[0]
    assert up["owner"] == "acct" and up["bucket"] == "seg1" and up["id"] == "seg1_3"
    assert base64.b64decode(up["message"]) == b"\x00\x01secret\xff"
    # read 回原始密文
    assert hot.read_chunk(owner="acct", segment_id="seg1", chunk_idx=3) == b"\x00\x01secret\xff"


def test_hot_read_missing_raises():
    hot = HttpHotTier("http://hub", requester=FakeHub())
    with pytest.raises(requests.HTTPError):
        hot.read_chunk(owner="acct", segment_id="seg1", chunk_idx=0)


def test_cold_read_slices_downloaded_blob():
    hub = FakeHub()
    blob = bytes(range(256))
    hub.store[("acct", "cid_xyz")] = blob          # 模拟已封段的整段密文
    cold = HttpColdTier("http://hub", "acct", requester=hub)
    assert cold.read(da_cid="cid_xyz", cipher_offset=100, cipher_len=20) == blob[100:120]


def test_cold_caches_download():
    hub = FakeHub()
    hub.store[("acct", "cid_xyz")] = b"x" * 50
    calls = {"n": 0}
    def counting(url, **k):
        calls["n"] += 1
        return hub(url, **k)
    cold = HttpColdTier("http://hub", "acct", requester=counting)
    cold.read(da_cid="cid_xyz", cipher_offset=0, cipher_len=10)
    cold.read(da_cid="cid_xyz", cipher_offset=10, cipher_len=10)
    assert calls["n"] == 1                          # 同段第二次读走缓存,不重复下载
