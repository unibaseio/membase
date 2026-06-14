"""写路径端到端测试(§8.1 握手):加密 -> 热层 -> 封段 -> 自签 AddPiece -> SEAL。

全部用内存 fake,不依赖 DA hub / 链 / 网络。验证:
  - 攒段阈值触发自动封段
  - provenance 写入 PUT×N + SEAL×segments,链可验证
  - hub(热层)全程只见密文,不见明文
  - 封段后按 manifest 读回单条消息明文(round-trip)
  - DAHubBackend 适配:seam 齐则走握手,缺则清晰报错
"""

import json
import tempfile
import os

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from membase.storage.encryptor import Encryptor, InMemoryKeyProvider
from membase.storage.provenance import ProvenanceLog
from membase.storage.segment import (
    SegmentBuffer, InMemoryHotTier, InMemorySealService, FakeRegistrar,
)


class EthSigner:
    def __init__(self): self.a = Account.create()
    @property
    def address(self): return self.a.address
    def sign_message(self, m): return self.a.sign_message(encode_defunct(text=m)).signature.hex()
    def valid_signature(self, m, s, addr): return Account.recover_message(encode_defunct(text=m), signature=s) == addr


def _build(seal_count=3, tmp=None):
    kp = InMemoryKeyProvider(); kp.generate("dk-v1")
    enc = Encryptor(kp)
    signer = EthSigner()
    path = os.path.join(tmp, "dom.jsonl")
    log = ProvenanceLog("dom", signer=signer, verifier=signer, path=path)
    hot = InMemoryHotTier()
    sealer = InMemorySealService(hot)
    reg = FakeRegistrar()
    buf = SegmentBuffer(owner="acct", domain_id="dom", encryptor=enc, provenance=log,
                        hot=hot, sealer=sealer, registrar=reg, seal_count=seal_count)
    return buf, log, hot, sealer, reg


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _msg(i):
    return json.dumps({"id": f"m{i}", "content": f"secret-memory-{i}", "role": "user"})


def test_handshake_seals_and_logs(tmp):
    buf, log, hot, sealer, reg = _build(seal_count=3, tmp=tmp)

    for i in range(7):
        buf.put(_msg(i).encode(), msg_id=f"m{i}")

    # 7 条 -> 在第 3、6 条触发两次封段,剩 1 条在 open 段
    assert len(buf.sealed_manifests) == 2
    assert len(reg.calls) == 2                      # 每封段一次自签 AddPiece
    # registrar 收到的 piece_core.name == da_cid
    for man, call in zip(buf.sealed_manifests, reg.calls):
        assert call["name"] == man["da_cid"]

    # provenance: 7 PUT + 2 SEAL,顺序正确,链可验证
    ops = [e["op"] for e in log.entries()]
    assert ops.count("PUT") == 7 and ops.count("SEAL") == 2
    ok, err = log.verify_chain(); assert ok, err

    # flush 封掉最后一段
    man3 = buf.flush()
    assert man3 is not None and len(buf.sealed_manifests) == 3
    assert [e["op"] for e in log.entries()].count("SEAL") == 3


def test_hub_never_sees_plaintext(tmp):
    buf, log, hot, sealer, reg = _build(seal_count=10, tmp=tmp)
    for i in range(5):
        buf.put(_msg(i).encode(), msg_id=f"m{i}")
    buf.flush()
    # 热层 + 封段存储里都不应出现明文片段
    raw_hot = b"".join(bytes(v) for v in hot._buf.values())
    raw_sealed = b"".join(sealer.sealed.values())
    for i in range(5):
        assert f"secret-memory-{i}".encode() not in raw_hot
        assert f"secret-memory-{i}".encode() not in raw_sealed


def test_read_back_each_message(tmp):
    buf, log, hot, sealer, reg = _build(seal_count=3, tmp=tmp)
    sent = {}
    for i in range(6):
        m = _msg(i); sent[f"m{i}"] = m
        buf.put(m.encode(), msg_id=f"m{i}")
    assert len(buf.sealed_manifests) == 2

    # 从每个封段按 manifest 读回单条,解密结果 == 原文
    for man in buf.sealed_manifests:
        for entry in man["msg_index"]:
            out = buf.read_message(man, entry["msg_id"])
            assert out.decode() == sent[entry["msg_id"]]


def test_manifest_anchors_da_cid_and_has_no_secrets(tmp):
    buf, log, hot, sealer, reg = _build(seal_count=2, tmp=tmp)
    buf.put(_msg(0).encode(), msg_id="m0")
    buf.put(_msg(1).encode(), msg_id="m1")
    man = buf.sealed_manifests[0]
    # SEAL 事件锚定该 manifest 的 da_cid
    seal_entry = [e for e in log.entries() if e["op"] == "SEAL"][0]
    assert seal_entry["payload"]["da_cid"] == man["da_cid"]
    # manifest 可 JSON 化且无明文 / 无密钥本体
    flat = json.dumps(man)
    assert "secret-memory" not in flat
    assert "domain_key" not in flat and "dk-v1" in flat  # 只有 key_id 版本标识


def test_da_backend_adapter_writepath(tmp):
    from membase.storage.da_backend import DAHubBackend
    kp = InMemoryKeyProvider(); kp.generate("dk-v1")
    enc = Encryptor(kp)
    signer = EthSigner()
    log = ProvenanceLog("dom", signer=signer, verifier=signer,
                        path=os.path.join(tmp, "d.jsonl"))
    hot = InMemoryHotTier(); sealer = InMemorySealService(hot); reg = FakeRegistrar()

    be = DAHubBackend("http://da-hub", encryptor=enc, provenance=log,
                      registrar=reg, hot=hot, sealer=sealer, domain_id="dom",
                      policy=(6, 4))
    res = be.upload_hub("acct", "mX", _msg(99))
    assert res["status"] == "completed" and "segment_id" in res
    man = be.flush()
    assert man["da_cid"].startswith("cid_")
    # 经适配器写入后,provenance 有 PUT + SEAL
    ops = [e["op"] for e in log.entries()]
    assert "PUT" in ops and "SEAL" in ops


def test_da_backend_missing_seam_raises(tmp):
    from membase.storage.da_backend import DAHubBackend
    be = DAHubBackend("http://da-hub")  # 无 seam
    with pytest.raises(NotImplementedError) as ei:
        be.upload_hub("acct", "mX", _msg(0))
    assert "seam" in str(ei.value).lower()
