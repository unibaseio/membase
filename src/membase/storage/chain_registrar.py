"""ChainPieceRegistrar — D3:客户端自签 AddPiece 上链(设计文档 §3.2 / §8.1 步骤3)。

去中心化正解:客户端掌握签名密钥、gas 出自客户端账户。复用 membase_chain 的 account。

⚠️ 关键边界(机密红线 + 正确性):AddPiece 的 piece 名参数需要 DA 的 BLS G1→solidity
   编码(da: G1StringInSolidity → bls.G1.SetBytes),以及按 DA 经济常量算的 cost。
   这些**留在 DA SDK(Go)侧**,绝不在 Python 重写曲线/参数。因此 SealService(Go hub)
   在 §8.1 步骤2 回传的 piece_core 必须已含:
     pn_solidity  : hex(已 solidity 编码的 G1 piece 名)   ← Go G1StringInSolidity 产物
     cost         : IncreaseAllowance 的 val(Go 按 size/policy/epoch/price 算好)
     price,size,expire,policy[N,K],streamer
   Python 这层只做**通用 web3 签名+广播**(IncreaseAllowance → AddPiece),无 BLS、无机密参数。

流程对齐 da-sdk-go/contract/v2/set.go::AddPiece:
   1. token.increaseAllowance(piece_addr, cost)  → 等待入块
   2. piece.addPiece(pn, price, size, expire, N, K, streamer) → 等待入块 → 返回 tx hash

web3 / 合约 / 账户均注入(默认从 env + membase_chain 构造),便于无链测试。
"""

from __future__ import annotations

import os
from typing import Optional

import logging
logger = logging.getLogger(__name__)

# 最小 ABI 片段(通用,无机密):ERC20 increaseAllowance + Piece.addPiece
ERC20_ALLOWANCE_ABI = [{
    "name": "increaseAllowance", "type": "function", "stateMutability": "nonpayable",
    "inputs": [{"name": "spender", "type": "address"}, {"name": "addedValue", "type": "uint256"}],
    "outputs": [{"name": "", "type": "bool"}],
}]
# 对齐 da: addPiece(bytes _pn, uint256 _price, uint64 _size, uint64 _expire, uint8 rsn, uint8 rsk, address _s)
PIECE_ADDPIECE_ABI = [{
    "name": "addPiece", "type": "function", "stateMutability": "nonpayable",
    "inputs": [
        {"name": "_pn", "type": "bytes"}, {"name": "_price", "type": "uint256"},
        {"name": "_size", "type": "uint64"}, {"name": "_expire", "type": "uint64"},
        {"name": "rsn", "type": "uint8"}, {"name": "rsk", "type": "uint8"},
        {"name": "_s", "type": "address"},
    ],
    "outputs": [],
}]


class ChainPieceRegistrar:
    """实现 segment.PieceRegistrar Protocol:sign_and_submit_add_piece(piece_core) -> tx hash。"""

    def __init__(self, *, w3, account_address, private_key,
                 piece_contract, token_contract, piece_addr,
                 gas_add_piece: int = 600000, gas_allowance: int = 120000,
                 tx_timeout: int = 180):
        self.w3 = w3
        self.account_address = account_address
        self.private_key = private_key
        self.piece = piece_contract       # web3 contract bound to Piece 合约地址 + ABI
        self.token = token_contract       # web3 contract bound to UB/token 地址 + ABI
        self.piece_addr = piece_addr      # IncreaseAllowance 的 spender = Piece 合约地址
        self.gas_add_piece = gas_add_piece
        self.gas_allowance = gas_allowance
        self.tx_timeout = tx_timeout

    def sign_and_submit_add_piece(self, piece_core: dict) -> str:
        n, k = piece_core["policy"]
        pn = piece_core["pn_solidity"]
        pn_bytes = bytes.fromhex(pn[2:] if pn.startswith("0x") else pn)

        # 1. 授权 token 给 Piece 合约(等待入块,faithful to Go: allowance 先于 addPiece)
        self._send(self.token.functions.increaseAllowance(self.piece_addr, int(piece_core["cost"])),
                   gas=self.gas_allowance, label="increaseAllowance")

        # 2. 自签 addPiece(CID),返回 tx hash
        tx_hash = self._send(
            self.piece.functions.addPiece(
                pn_bytes, int(piece_core["price"]), int(piece_core["size"]),
                int(piece_core["expire"]), int(n), int(k), piece_core["streamer"]),
            gas=self.gas_add_piece, label="addPiece")
        logger.info("AddPiece submitted: %s -> %s", piece_core.get("name"), tx_hash)
        return tx_hash

    def _send(self, fn, *, gas: int, label: str) -> str:
        """通用:build → 客户端私钥签 → 广播 → 等待入块。每次取 pending nonce(faithful MakeAuth)。"""
        nonce = self.w3.eth.get_transaction_count(self.account_address, "pending")
        tx = fn.build_transaction({
            "from": self.account_address, "nonce": nonce,
            "gas": gas, "gasPrice": self.w3.eth.gas_price,
        })
        signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        h = self.w3.eth.send_raw_transaction(raw)
        receipt = self.w3.eth.wait_for_transaction_receipt(h, timeout=self.tx_timeout)
        if receipt and getattr(receipt, "get", lambda *_: None)("status") == 0:
            raise RuntimeError(f"{label} tx reverted: {h.hex() if hasattr(h, 'hex') else h}")
        return h.hex() if hasattr(h, "hex") else str(h)


class HubProxyRegistrar:
    """v1(默认,早期/web2 友好):hub 代签并支付 gas 上链;客户端零 gas、零密钥。

    /api/seal 在 register=hub 模式下已由 hub 完成 sdk.Upload + AddPiece,piece_core 带
    add_piece_tx;本 registrar 仅回传该 tx(链上已注册,无需客户端再签)。满足 PieceRegistrar。

    ⚠️ 取舍:on-chain piece 归属/保证金落在 hub 账户(内容仍客户端加密 → 数据主权不变);
       渐进去中心化时切到 ChainPieceRegistrar(v2 客户端自签),或走 meta-tx/EIP-2771
       (用户免 gas 但链上归属仍是用户)作为中间路线。
    """

    def sign_and_submit_add_piece(self, piece_core: dict) -> str:
        tx = piece_core.get("add_piece_tx")
        if not tx:
            raise RuntimeError(
                "hub 代签模式需 /api/seal 以 register=hub 返回 add_piece_tx;"
                "piece_core 缺该字段")
        logger.info("AddPiece via hub proxy (v1): %s -> %s", piece_core.get("name"), tx)
        return tx


def build_registrar():
    """按 env MEMBASE_DA_REGISTER 选注册模式:
      'hub'(默认,v1 早期/web2)-> HubProxyRegistrar(hub 代签)
      'client'(v2 渐进去中心化)-> ChainPieceRegistrar(客户端自签,需链 env)
    """
    mode = os.getenv("MEMBASE_DA_REGISTER", "hub").lower()
    if mode == "client":
        logger.info("AddPiece register mode: client self-sign (v2)")
        return build_chain_registrar()
    logger.info("AddPiece register mode: hub proxy (v1)")
    return HubProxyRegistrar()


def build_chain_registrar() -> ChainPieceRegistrar:
    """从 env + membase_chain 构造(惰性,缺 env 时报清晰错误)。

    env: MEMBASE_DA_PIECE_ADDR(Piece 合约)、MEMBASE_DA_TOKEN_ADDR(token 合约)。
    复用 membase_chain 的 w3 / wallet_address / private_key(同一签名账户,D3)。
    """
    piece_addr = os.getenv("MEMBASE_DA_PIECE_ADDR")
    token_addr = os.getenv("MEMBASE_DA_TOKEN_ADDR")
    if not piece_addr or not token_addr:
        raise RuntimeError("MEMBASE_DA_PIECE_ADDR / MEMBASE_DA_TOKEN_ADDR 未设置")
    from membase.chain.chain import membase_chain  # 惰性:缺链 env 才退出进程
    w3 = membase_chain.w3
    piece_addr = w3.to_checksum_address(piece_addr)
    token_addr = w3.to_checksum_address(token_addr)
    return ChainPieceRegistrar(
        w3=w3, account_address=membase_chain.wallet_address,
        private_key=membase_chain.private_key,
        piece_contract=w3.eth.contract(address=piece_addr, abi=PIECE_ADDPIECE_ABI),
        token_contract=w3.eth.contract(address=token_addr, abi=ERC20_ALLOWANCE_ABI),
        piece_addr=piece_addr,
    )
