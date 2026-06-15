"""ChainPieceRegistrar 测试(D3):通用 web3 签名+广播,mock 链,不触真网络。

验证:allowance→addPiece 两笔顺序、参数映射、nonce 递增(pending)、用客户端私钥签、
回滚处理、满足 PieceRegistrar Protocol。无 BLS / 无机密参数在 Python 侧。
"""

from types import SimpleNamespace
import pytest

from membase.storage.chain_registrar import (
    ChainPieceRegistrar, HubProxyRegistrar, build_registrar,
)
from membase.storage.segment import PieceRegistrar, InMemoryHotTier, InMemorySealService


# ----- mock web3 ------------------------------------------------------------ #

class FakeFn:
    def __init__(self, rec, name, args):
        self.rec, self.name, self.args = rec, name, args
    def build_transaction(self, params):
        self.rec.append((self.name, self.args, dict(params)))
        return {"name": self.name, **params}


class FakeFunctions:
    def __init__(self, rec): self.rec = rec
    def increaseAllowance(self, spender, value):
        return FakeFn(self.rec, "increaseAllowance", (spender, value))
    def addPiece(self, pn, price, size, expire, n, k, streamer):
        return FakeFn(self.rec, "addPiece", (pn, price, size, expire, n, k, streamer))


class FakeContract:
    def __init__(self, rec): self.functions = FakeFunctions(rec)


class FakeHash:
    def __init__(self, b): self.b = b
    def hex(self): return "0x" + self.b


class FakeAccount:
    def __init__(self, rec): self.rec = rec
    def sign_transaction(self, tx, key):
        self.rec.append(("sign", tx["name"], tx["nonce"], key))
        return SimpleNamespace(raw_transaction=b"raw:" + tx["name"].encode())


class FakeEth:
    def __init__(self, rec, status=1):
        self.rec, self.account = rec, FakeAccount(rec)
        self._nonce, self._sent, self.status = 0, 0, status
    def get_transaction_count(self, addr, tag): return self._nonce
    @property
    def gas_price(self): return 10 ** 9
    def send_raw_transaction(self, raw):
        self._sent += 1
        self._nonce += 1  # 模拟 pending:下一笔 nonce 自增
        return FakeHash(f"{self._sent:064x}")
    def wait_for_transaction_receipt(self, h, timeout=None):
        return {"status": self.status}


class FakeW3:
    def __init__(self, rec, status=1): self.eth = FakeEth(rec, status)


def _registrar(rec, status=1):
    return ChainPieceRegistrar(
        w3=FakeW3(rec, status), account_address="0xACCT", private_key="0xKEY",
        piece_contract=FakeContract(rec), token_contract=FakeContract(rec),
        piece_addr="0xPIECE")


PIECE_CORE = {
    "name": "cid_abc", "pn_solidity": "deadbeef", "cost": 1000,
    "price": 100, "size": 4096, "expire": 9999, "policy": [6, 4], "streamer": "0xSTREAM",
}


def test_satisfies_protocol():
    assert isinstance(_registrar([]), PieceRegistrar)


def test_allowance_then_addpiece_order_and_args():
    rec = []
    tx = _registrar(rec).sign_and_submit_add_piece(PIECE_CORE)
    fns = [r for r in rec if r[0] in ("increaseAllowance", "addPiece")]
    assert [f[0] for f in fns] == ["increaseAllowance", "addPiece"]   # 顺序
    # allowance(spender=piece_addr, cost)
    assert fns[0][1] == ("0xPIECE", 1000)
    # addPiece(pn_bytes, price, size, expire, N, K, streamer)
    pn, price, size, expire, n, k, streamer = fns[1][1]
    assert pn == bytes.fromhex("deadbeef")        # hex 解码,无 BLS
    assert (price, size, expire, n, k, streamer) == (100, 4096, 9999, 6, 4, "0xSTREAM")
    # 返回 addPiece(第2笔)的 hash
    assert tx == "0x" + f"{2:064x}"


def test_nonce_increments_across_txs():
    rec = []
    _registrar(rec).sign_and_submit_add_piece(PIECE_CORE)
    nonces = [r[2] for r in rec if r[0] == "sign"]
    assert nonces == [0, 1]                        # allowance=0, addPiece=1(pending 递增)


def test_signs_with_client_key():
    rec = []
    _registrar(rec).sign_and_submit_add_piece(PIECE_CORE)
    assert all(r[3] == "0xKEY" for r in rec if r[0] == "sign")   # D3:客户端私钥


def test_revert_raises():
    with pytest.raises(RuntimeError):
        _registrar([], status=0).sign_and_submit_add_piece(PIECE_CORE)


def test_consumes_sealservice_piece_core():
    # 端到端形态:fake SealService 产出的 piece_core 能被 registrar 直接消费
    hot = InMemoryHotTier()
    hot.push(owner="o", segment_id="s", chunk_idx=0, cipher_bytes=b"x" * 32)
    res = InMemorySealService(hot).seal(owner="o", segment_id="s", policy=(6, 4))
    rec = []
    tx = _registrar(rec).sign_and_submit_add_piece(res["piece_core"])
    assert tx.startswith("0x")
    # cost / pn_solidity 来自 SealService(Go 侧),Python 不算
    allowance = next(r for r in rec if r[0] == "increaseAllowance")
    assert allowance[1][1] == res["piece_core"]["cost"]


# ----- v1:hub 代签(默认,早期/web2 友好) -------------------------------- #

def test_hub_proxy_satisfies_protocol():
    assert isinstance(HubProxyRegistrar(), PieceRegistrar)


def test_hub_proxy_returns_hub_tx_no_client_signing():
    # v1:hub 已在 /api/seal 完成 AddPiece,registrar 仅回传 tx,客户端不签任何交易
    reg = HubProxyRegistrar()
    assert reg.sign_and_submit_add_piece({"name": "cid", "add_piece_tx": "0xHUBTX"}) == "0xHUBTX"


def test_hub_proxy_missing_tx_raises():
    with pytest.raises(RuntimeError):
        HubProxyRegistrar().sign_and_submit_add_piece({"name": "cid"})  # 缺 add_piece_tx


def test_hub_proxy_consumes_sealservice_v1():
    hot = InMemoryHotTier()
    hot.push(owner="o", segment_id="s", chunk_idx=0, cipher_bytes=b"y" * 16)
    res = InMemorySealService(hot).seal(owner="o", segment_id="s", policy=(6, 4))
    tx = HubProxyRegistrar().sign_and_submit_add_piece(res["piece_core"])
    assert tx == res["piece_core"]["add_piece_tx"]


def test_build_registrar_mode_select(monkeypatch):
    monkeypatch.delenv("MEMBASE_DA_REGISTER", raising=False)
    assert isinstance(build_registrar(), HubProxyRegistrar)          # 默认 v1 hub 代签
    monkeypatch.setenv("MEMBASE_DA_REGISTER", "client")
    # client 模式需链 env;缺则 build_chain_registrar 报错(此处只验路由到 client 分支)
    with pytest.raises(Exception):
        build_registrar()
