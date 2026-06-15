"""Encryptor — 客户端信封加密 + 分块(D2 / 设计文档 §5)。

为什么加密不破坏可验证(§5.1):DA 的 KZG 承诺是对**上传字节**算的。只要上传的是
密文,DA 的编码证明/epoch 证明/挑战博弈照常成立——加密与可验证完全正交。

密钥模型(§5.2,domain 共享):
  - 每个 domain 持一把对称 domain key (DK),由 key_id 标识版本(§5.4 轮换)。
  - 每段派生独立数据密钥:SK = HKDF-SHA256(DK, info="membase-seg:"+segment_id)。
  - 段内容用 AEAD(SK, nonce) 加密。授权 = 交付 DK(链上 buy 后 ECIES 封装投递),
    不是服务端 ACL ⇒ 恶意 hub/store/gateway 都读不到明文。

分块(§5.3,支持单条读):整段一把 nonce 会导致读单条也要解全段。改为段内分 chunk,
每块独立 AEAD(nonce = base_nonce ‖ chunk_idx);manifest 记录每条消息落在哪个 chunk
+ offset,读单条只解相关 chunk。

实现取舍:
  - AEAD = AES-256-GCM(`cryptography` 库,已在 pyproject 显式声明,审计充分,与
    keyring 的 X25519/HKDF 统一在同一后端)。
  - nonce = 4B 每段随机 base_nonce ‖ 8B chunk_idx(共 12B)。base_nonce 每次加密随机,
    即便同 segment_id 重复加密(SK 相同)也不会 nonce 复用 ⇒ 规避 GCM nonce 重用灾难。
  - GCM tag(16B)由 AESGCM 自动附在每块密文尾部(ct‖tag)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidTag

import logging
logger = logging.getLogger(__name__)

ALG = "AES-256-GCM"
TAG_LEN = 16
DEFAULT_CHUNK_SIZE = 128 * 1024  # §5.3 初值;块越小定位越细但 tag 开销越大
_HKDF_SALT = b"membase-da-provenance-v1"


# --------------------------------------------------------------------------- #
# 密钥提供者 seam
# --------------------------------------------------------------------------- #

@runtime_checkable
class KeyProvider(Protocol):
    """按 key_id 返回 32 字节 domain key(DK)。真实实现接 buy() 授权 + ECIES 投递。"""

    def domain_key(self, key_id: str) -> bytes: ...


class InMemoryKeyProvider:
    """开发/测试用:进程内持有 DK。生产替换为接链上授权的实现。"""

    def __init__(self, keys: Optional[dict] = None):
        self._keys = dict(keys or {})

    def add_key(self, key_id: str, dk: bytes):
        if len(dk) != 32:
            raise ValueError("domain key 必须 32 字节")
        self._keys[key_id] = dk

    def generate(self, key_id: str) -> bytes:
        dk = os.urandom(32)
        self._keys[key_id] = dk
        return dk

    def domain_key(self, key_id: str) -> bytes:
        if key_id not in self._keys:
            raise KeyError(f"no domain key for key_id={key_id!r}")
        return self._keys[key_id]


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class ChunkMeta:
    idx: int
    plain_offset: int   # 明文中该块起始偏移
    plain_len: int
    cipher_offset: int  # 拼接密文中该块起始偏移
    cipher_len: int     # = plain_len + TAG_LEN


@dataclass
class EncryptedSegment:
    alg: str
    key_id: str
    segment_id: str
    chunk_size: int
    base_nonce: bytes          # 每段随机 4B 前缀
    size_plain: int
    chunks: list               # list[ChunkMeta]
    ciphertext: bytes          # 各块密文(含 tag)顺序拼接

    def manifest(self) -> dict:
        """§3.4 manifest 的 enc 段 + chunk 索引(不含密文、不含密钥)。"""
        return {
            "enc": {
                "alg": self.alg,
                "key_id": self.key_id,
                "base_nonce": self.base_nonce.hex(),
                "chunk_size": self.chunk_size,
            },
            "size_plain": self.size_plain,
            "size_cipher": len(self.ciphertext),
            "chunks": [
                {"idx": c.idx, "plain_offset": c.plain_offset, "plain_len": c.plain_len,
                 "cipher_offset": c.cipher_offset, "cipher_len": c.cipher_len}
                for c in self.chunks
            ],
        }


# --------------------------------------------------------------------------- #
# Encryptor
# --------------------------------------------------------------------------- #

class Encryptor:
    def __init__(self, key_provider: KeyProvider, *, chunk_size: int = DEFAULT_CHUNK_SIZE):
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须为正")
        self._kp = key_provider
        self.chunk_size = chunk_size

    def _sk(self, key_id: str, segment_id: str) -> bytes:
        dk = self._kp.domain_key(key_id)
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=_HKDF_SALT,
                    info=b"membase-seg:" + segment_id.encode("utf-8")).derive(dk)

    @staticmethod
    def _nonce(base_nonce: bytes, chunk_idx: int) -> bytes:
        return base_nonce + chunk_idx.to_bytes(8, "big")  # 4 + 8 = 12B

    # ----- 加密 -------------------------------------------------------------- #

    def encrypt(self, plaintext: bytes, *, key_id: str, segment_id: str) -> EncryptedSegment:
        sk = self._sk(key_id, segment_id)
        base_nonce = os.urandom(4)
        chunks: list = []
        out = bytearray()
        n = len(plaintext)
        idx = 0
        plain_off = 0
        while plain_off < n or (n == 0 and idx == 0):
            block = plaintext[plain_off: plain_off + self.chunk_size]
            blob = AESGCM(sk).encrypt(self._nonce(base_nonce, idx), block, None)  # ct‖tag
            chunks.append(ChunkMeta(
                idx=idx, plain_offset=plain_off, plain_len=len(block),
                cipher_offset=len(out), cipher_len=len(blob),
            ))
            out.extend(blob)
            plain_off += len(block)
            idx += 1
            if n == 0:
                break  # 空明文也产出一个空块,保持可逆

        return EncryptedSegment(
            alg=ALG, key_id=key_id, segment_id=segment_id, chunk_size=self.chunk_size,
            base_nonce=base_nonce, size_plain=n, chunks=chunks, ciphertext=bytes(out),
        )

    # ----- 解密 -------------------------------------------------------------- #

    def decrypt(self, seg: EncryptedSegment) -> bytes:
        """整段解密(读全量时用)。"""
        sk = self._sk(seg.key_id, seg.segment_id)
        out = bytearray()
        for c in seg.chunks:
            blob = seg.ciphertext[c.cipher_offset: c.cipher_offset + c.cipher_len]
            out.extend(self._decrypt_blob(sk, seg.base_nonce, c.idx, blob))
        return bytes(out)

    def decrypt_chunk(self, manifest_enc: dict, chunk_meta: dict, cipher_bytes: bytes,
                      *, key_id: str, segment_id: str) -> bytes:
        """只解一个 chunk(读单条消息时用,§7.1 快路径)。

        manifest_enc: manifest()["enc"];chunk_meta: manifest()["chunks"][i];
        cipher_bytes: 该 chunk 的密文(从 DA 按 cipher_offset/cipher_len 取回)。
        """
        sk = self._sk(key_id, segment_id)
        base_nonce = bytes.fromhex(manifest_enc["base_nonce"])
        return self._decrypt_blob(sk, base_nonce, chunk_meta["idx"], cipher_bytes)

    @staticmethod
    def _decrypt_blob(sk: bytes, base_nonce: bytes, idx: int, blob: bytes) -> bytes:
        try:
            return AESGCM(sk).decrypt(base_nonce + idx.to_bytes(8, "big"), blob, None)
        except InvalidTag:
            raise ValueError("AEAD authentication failed")  # 稳定异常类型给调用方

    def segment_writer(self, *, key_id: str, segment_id: str) -> "SegmentWriter":
        """增量式写:逐条消息 add() 加密成一个 chunk,共享同一段 SK / base_nonce。
        用于热层模型(§8.1 步骤 1):客户端边收消息边加密推送,hub 后拼接密文成段。"""
        return SegmentWriter(self, key_id=key_id, segment_id=segment_id)

    # ----- 定位:读单条消息时算出需要哪些 chunk ------------------------------ #

    @staticmethod
    def chunks_for_range(chunks: list, offset: int, length: int) -> list:
        """返回覆盖明文区间 [offset, offset+length) 的 chunk(dict 或 ChunkMeta)。"""
        end = offset + length
        hit = []
        for c in chunks:
            co = c["plain_offset"] if isinstance(c, dict) else c.plain_offset
            cl = c["plain_len"] if isinstance(c, dict) else c.plain_len
            if co < end and (co + cl) > offset:
                hit.append(c)
        return hit


class SegmentWriter:
    """一个正在形成的段的增量加密器:每 add() 一条消息 → 一个 chunk(共享段 SK)。

    1 消息 ↔ 1 chunk,使"读单条 = 解一个 chunk"达到最细粒度。base_nonce 每段随机,
    chunk_idx 单调递增 ⇒ nonce 唯一,GCM 安全。
    """

    def __init__(self, enc: "Encryptor", *, key_id: str, segment_id: str):
        self.key_id = key_id
        self.segment_id = segment_id
        self._sk = enc._sk(key_id, segment_id)
        self.base_nonce = os.urandom(4)
        self._idx = 0
        self._plain_off = 0
        self._cipher_off = 0
        self.chunks: list = []

    def add(self, block: bytes):
        """加密一条消息为一个 chunk,返回 (该 chunk 密文, ChunkMeta)。"""
        blob = AESGCM(self._sk).encrypt(
            self.base_nonce + self._idx.to_bytes(8, "big"), block, None)  # ct‖tag
        cm = ChunkMeta(idx=self._idx, plain_offset=self._plain_off, plain_len=len(block),
                       cipher_offset=self._cipher_off, cipher_len=len(blob))
        self.chunks.append(cm)
        self._idx += 1
        self._plain_off += len(block)
        self._cipher_off += len(blob)
        return blob, cm

    @property
    def size_plain(self) -> int:
        return self._plain_off

    @property
    def size_cipher(self) -> int:
        return self._cipher_off

    def manifest_enc(self) -> dict:
        """供 SegmentManifest 的 enc 段(无密钥)。chunk_size 不适用(逐消息变长)。"""
        return {"alg": ALG, "key_id": self.key_id, "base_nonce": self.base_nonce.hex()}
