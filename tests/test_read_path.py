"""读路径端到端测试(§7):manifest 索引 + 冷/热层取密文 + 解密 + 顺序 + SUPERSEDE。"""

import json
import os
import tempfile

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from membase.storage.encryptor import Encryptor, InMemoryKeyProvider
from membase.storage.provenance import ProvenanceLog
from membase.storage.segment import (
    SegmentBuffer, InMemoryHotTier, InMemorySealService, FakeRegistrar,
)
from membase.storage.reader import Reader
from membase.storage.da_backend import DAHubBackend


class EthSigner:
    def __init__(self): self.a = Account.create()
    @property
    def address(self): return self.a.address
    def sign_message(self, m): return self.a.sign_message(encode_defunct(text=m)).signature.hex()
    def valid_signature(self, m, s, addr): return Account.recover_message(encode_defunct(text=m), signature=s) == addr


@pytest.fixture
def env():
    with tempfile.TemporaryDirectory() as d:
        kp = InMemoryKeyProvider(); kp.generate("dk-v1")
        enc = Encryptor(kp)
        signer = EthSigner()
        log = ProvenanceLog("dom", signer=signer, verifier=signer,
                            path=os.path.join(d, "log.jsonl"))
        hot = InMemoryHotTier(); sealer = InMemorySealService(hot); reg = FakeRegistrar()
        yield {"d": d, "kp": kp, "enc": enc, "signer": signer, "log": log,
               "hot": hot, "sealer": sealer, "reg": reg}


def _buf(env, **kw):
    return SegmentBuffer(owner="acct", domain_id="dom", encryptor=env["enc"],
                         provenance=env["log"], hot=env["hot"], sealer=env["sealer"],
                         registrar=env["reg"], **kw)


def _msg(conv, i):
    return json.dumps({"id": f"{conv}_{i}", "name": "a",
                       "content": f"msg-{conv}-{i}", "role": "user"})


def test_read_back_across_cold_and_hot(env):
    # seal_count=2:conv c0 写 3 条 -> 1 段已封(冷层 2 条)+ 在途 1 条(热层)
    buf = _buf(env, seal_count=2)
    sent = {}
    for i in range(3):
        m = _msg("c0", i); sent[f"c0_{i}"] = m
        buf.put(m.encode(), msg_id=f"c0_{i}")
    assert len(buf.sealed_manifests) == 1  # 第3条还在途

    reader = Reader(buf.all_manifests(), segment_reader=buf, provenance=env["log"])
    got = reader.get_conversation("c0")
    assert got == [sent["c0_0"], sent["c0_1"], sent["c0_2"]]  # 顺序正确,跨冷+热


def test_multi_conversation_isolation(env):
    buf = _buf(env, seal_count=100)
    buf.put(_msg("a", 0).encode(), msg_id="a_0")
    buf.put(_msg("b", 0).encode(), msg_id="b_0")
    buf.put(_msg("a", 1).encode(), msg_id="a_1")
    buf.flush()
    reader = Reader(buf.all_manifests(), segment_reader=buf, provenance=env["log"])
    assert set(reader.conversations()) == {"a", "b"}
    a = reader.get_conversation("a")
    assert [json.loads(s)["content"] for s in a] == ["msg-a-0", "msg-a-1"]
    assert len(reader.get_conversation("b")) == 1


def test_supersession_skips_corrected(env):
    buf = _buf(env, seal_count=100)
    for i in range(3):
        buf.put(_msg("c", i).encode(), msg_id=f"c_{i}")
    buf.supersede("c_1", reason="fixed")   # 订正第 2 条
    buf.flush()
    reader = Reader(buf.all_manifests(), segment_reader=buf, provenance=env["log"])
    got = [json.loads(s)["content"] for s in reader.get_conversation("c")]
    assert got == ["msg-c-0", "msg-c-2"]   # c_1 被跳过


def test_manifest_anchor_verification(env):
    buf = _buf(env, seal_count=2)
    buf.put(_msg("c", 0).encode(), msg_id="c_0")
    buf.put(_msg("c", 1).encode(), msg_id="c_1")  # 自动封段
    reader = Reader(buf.all_manifests(), segment_reader=buf, provenance=env["log"])
    ok, err = reader.verify_anchors()
    assert ok, err
    # 篡改 manifest_hash -> 锚定校验失败
    buf.sealed_manifests[0]["manifest_hash"] = "deadbeef"
    reader2 = Reader(buf.all_manifests(), segment_reader=buf, provenance=env["log"])
    ok2, err2 = reader2.verify_anchors()
    assert not ok2 and "mismatch" in err2


def test_durable_reload_then_read(env):
    # manifests 持久化 -> 新进程 buffer 从文件恢复封段并可读
    mpath = os.path.join(env["d"], "manifests.jsonl")
    buf = _buf(env, seal_count=2, manifests_path=mpath)
    sent = {}
    for i in range(2):
        m = _msg("c", i); sent[f"c_{i}"] = m
        buf.put(m.encode(), msg_id=f"c_{i}")   # 第2条触发封段并持久化 manifest
    assert os.path.exists(mpath)

    # 新 buffer:复用同一 hot/sealer(=DA 存储仍在),从 manifests 文件恢复
    buf2 = _buf(env, seal_count=2, manifests_path=mpath)
    assert len(buf2.sealed_manifests) == 1
    reader = Reader(buf2.all_manifests(), segment_reader=buf2, provenance=env["log"])
    assert reader.get_conversation("c") == [sent["c_0"], sent["c_1"]]


def test_da_backend_read_roundtrip(env):
    be = DAHubBackend("http://da-hub", encryptor=env["enc"], provenance=env["log"],
                      registrar=env["reg"], hot=env["hot"], sealer=env["sealer"],
                      domain_id="dom")
    sent = [_msg("k", i) for i in range(3)]
    for i, m in enumerate(sent):
        be.upload_hub("acct", f"k_{i}", m)
    # 经适配器读:get_conversation 有序、download_hub 单条
    assert be.get_conversation("acct", "k") == sent
    assert be.download_hub("acct", "k_1").decode() == sent[1]
    assert "k" in be.list_conversations("acct")
