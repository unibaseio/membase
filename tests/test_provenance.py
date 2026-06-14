"""ProvenanceLog 测试 —— 用真实 EIP-191 签名(eth_account),不依赖链环境。"""

import os
import json
import tempfile

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from membase.storage.provenance import ProvenanceLog


class EthSigner:
    """真实 EIP-191 Signer+Verifier,镜像 membase_chain 的 sign/verify 语义。"""

    def __init__(self, key=None):
        self._acct = Account.create() if key is None else Account.from_key(key)

    @property
    def address(self) -> str:
        return self._acct.address

    def sign_message(self, message: str) -> str:
        signed = self._acct.sign_message(encode_defunct(text=message))
        return signed.signature.hex()

    def valid_signature(self, message: str, signature: str, address: str) -> bool:
        rec = Account.recover_message(encode_defunct(text=message), signature=signature)
        return rec == address


@pytest.fixture
def tmp_log():
    s = EthSigner()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "domain.jsonl")
        yield ProvenanceLog("domain1", signer=s, verifier=s, path=path), s, path


def test_append_and_chain_links(tmp_log):
    log, signer, _ = tmp_log
    assert log.head() is None and len(log) == 0

    h0 = log.append(op="PUT", payload={"msg_id": "a", "type": "stm"})
    h1 = log.append(op="SEAL", payload={"segment_id": "s0", "da_cid": "cid0",
                                        "msg_range": {"start_seq": 0, "end_seq": 0}})
    h2 = log.append(op="SUPERSEDE", payload={"target": 0, "reason": "corrected"})

    assert log.head() == h2 and len(log) == 3
    entries = list(log.entries())
    assert [e["seq"] for e in entries] == [0, 1, 2]
    # prev_hash 链接
    assert entries[0]["prev_hash"] == ""
    assert entries[1]["prev_hash"] == h0
    assert entries[2]["prev_hash"] == h1
    # 作者地址 = signer
    assert all(e["author_addr"] == signer.address for e in entries)


def test_verify_chain_ok(tmp_log):
    log, _, _ = tmp_log
    log.append(op="PUT", payload={"msg_id": "a"})
    log.append(op="PUT", payload={"msg_id": "b"})
    ok, err = log.verify_chain()
    assert ok and err is None


def test_tamper_payload_breaks_chain(tmp_log):
    log, _, path = tmp_log
    log.append(op="PUT", payload={"msg_id": "a"})
    log.append(op="PUT", payload={"msg_id": "b"})

    # 篡改第 0 条 payload,保留其它字段
    lines = open(path).read().splitlines()
    e0 = json.loads(lines[0])
    e0["payload"] = {"msg_id": "EVIL"}
    lines[0] = json.dumps(e0, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    open(path, "w").write("\n".join(lines) + "\n")

    ok, err = log.verify_chain()
    assert not ok
    # 要么 entry_hash 对不上,要么签名失效——总之被抓到
    assert "seq 0" in err or "mismatch" in err or "signature" in err


def test_tamper_breaks_prev_hash_linkage(tmp_log):
    log, signer, path = tmp_log
    log.append(op="PUT", payload={"msg_id": "a"})
    log.append(op="PUT", payload={"msg_id": "b"})

    # 用合法签名重写第 0 条(改 payload 并重签),但其 entry_hash 会变 → 断开第 1 条 prev_hash
    lines = open(path).read().splitlines()
    e0 = json.loads(lines[0])
    content = {k: e0[k] for k in ("seq", "prev_hash", "op", "payload", "author_addr", "ts")}
    content["payload"] = {"msg_id": "EVIL"}
    import membase.storage.provenance as pv
    sig = signer.sign_message(pv._canonical(content))
    new_e0 = {**content, "author_sig": sig}
    new_e0["entry_hash"] = pv._sha256_hex(pv._canonical(new_e0))
    lines[0] = pv._canonical(new_e0)
    open(path, "w").write("\n".join(lines) + "\n")

    ok, err = log.verify_chain()
    assert not ok
    assert "prev_hash mismatch at seq 1" in err


def test_persistence_reload(tmp_log):
    log, signer, path = tmp_log
    log.append(op="PUT", payload={"msg_id": "a"})
    h1 = log.append(op="PUT", payload={"msg_id": "b"})

    # 新实例从同一文件恢复 head/seq
    reopened = ProvenanceLog("domain1", signer=signer, verifier=signer, path=path)
    assert reopened.head() == h1
    assert len(reopened) == 2
    h2 = reopened.append(op="PUT", payload={"msg_id": "c"})
    assert reopened.head() == h2
    ok, err = reopened.verify_chain()
    assert ok, err
    assert len(reopened) == 3


def test_unknown_op_rejected(tmp_log):
    log, _, _ = tmp_log
    with pytest.raises(ValueError):
        log.append(op="WRITE", payload={})


def test_author_must_be_signer(tmp_log):
    log, _, _ = tmp_log
    with pytest.raises(ValueError):
        log.append(op="PUT", payload={}, author_addr="0xdeadbeef")
