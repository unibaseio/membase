"""EciesKeyProvider — 真 domain key 投递(D2 / 设计文档 §5.2、§5.4)。

替换 encryptor.InMemoryKeyProvider:实现"授权 = 交付 DK"的去中心化模型。
- 每个 domain 持一把对称 domain key (DK),由 key_id 标识版本(§5.4 轮换)。
- 授权方(owner)用被授权方的**加密公钥**把 DK 封装(ECIES 密封盒)→ 经 hub 中转密文投递。
  链上 buy(memory_uuid, agent_uuid) 授权(membase_chain 已有);本模块负责密钥的密码学投递。
- 恶意 hub/store/gateway 只见密文 envelope,拿不到 DK。

ECIES(密封盒,等价 NaCl box / EIP-style):
  seal:  临时 X25519 keypair → ECDH(eph_priv, recipient_pub) → HKDF-SHA256 → AES-256-GCM 加密
  open:  ECDH(recipient_priv, eph_pub) → 同一 HKDF/SK → AES-256-GCM 解密
统一用 `cryptography` 后端(与 encryptor 同库)。

注:加密身份是独立的 X25519 keypair(类似 MetaMask 的 encryption pubkey),与 agent 的
secp256k1 签名密钥分离 —— 不在 Python 重写 secp256k1 ECDH(避免引入额外曲线依赖/机密)。
"""

from __future__ import annotations

import os
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

import logging
logger = logging.getLogger(__name__)

_HKDF_SALT = b"membase-ecies-v1"
_HKDF_INFO = b"membase-domain-key-wrap"


def generate_encryption_key() -> bytes:
    """生成一把 X25519 加密私钥(32B raw)。"""
    return X25519PrivateKey.generate().private_bytes_raw()


def encryption_public_key(priv: bytes) -> bytes:
    """从私钥导出加密公钥(32B raw),供对方 seal 时用 / 可公开发布。"""
    return X25519PrivateKey.from_private_bytes(priv).public_key().public_bytes_raw()


def seal(plaintext: bytes, recipient_pub: bytes) -> dict:
    """ECIES 密封:返回 {epk, nonce, ct}(均 hex)。只有 recipient 私钥能 open。"""
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(recipient_pub))
    sk = HKDF(algorithm=hashes.SHA256(), length=32, salt=_HKDF_SALT, info=_HKDF_INFO).derive(shared)
    nonce = os.urandom(12)
    ct = AESGCM(sk).encrypt(nonce, plaintext, None)
    return {"epk": eph.public_key().public_bytes_raw().hex(),
            "nonce": nonce.hex(), "ct": ct.hex()}


def open_sealed(envelope: dict, recipient_priv: bytes) -> bytes:
    """用 recipient 私钥解封 seal() 的 envelope。tag/密钥不符抛 ValueError。"""
    from cryptography.exceptions import InvalidTag
    priv = X25519PrivateKey.from_private_bytes(recipient_priv)
    shared = priv.exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(envelope["epk"])))
    sk = HKDF(algorithm=hashes.SHA256(), length=32, salt=_HKDF_SALT, info=_HKDF_INFO).derive(shared)
    try:
        return AESGCM(sk).decrypt(bytes.fromhex(envelope["nonce"]), bytes.fromhex(envelope["ct"]), None)
    except InvalidTag:
        raise ValueError("ECIES open failed (wrong key or tampered envelope)")


class EciesKeyProvider:
    """满足 encryptor.KeyProvider Protocol:domain_key(key_id) -> 32B DK。

    同时承担"授权 = 交付 DK":
      owner 侧:create_domain_key / grant(key_id, recipient_pub) -> envelope
      grantee 侧:accept(key_id, envelope) 后 domain_key(key_id) 即可用
    """

    def __init__(self, encryption_priv: Optional[bytes] = None):
        # 本 agent 的加密身份(X25519)。grantee 用它解封收到的 DK。
        self.encryption_priv = encryption_priv or generate_encryption_key()
        self._keys: dict = {}  # key_id -> DK(32B)

    @property
    def encryption_pub(self) -> bytes:
        return encryption_public_key(self.encryption_priv)

    # ----- owner 侧 ----------------------------------------------------------- #

    def create_domain_key(self, key_id: str) -> bytes:
        dk = os.urandom(32)
        self._keys[key_id] = dk
        return dk

    def grant(self, key_id: str, recipient_pub: bytes) -> dict:
        """把某 domain 的 DK 用 recipient 加密公钥封装,返回可经 hub 投递的密文 envelope。

        链上授权(buy)由调用方在投递前完成 —— 见 auth.buy_auth_onchain。
        """
        if key_id not in self._keys:
            raise KeyError(f"no domain key for key_id={key_id!r}")
        return seal(self._keys[key_id], recipient_pub)

    # ----- grantee 侧 --------------------------------------------------------- #

    def accept(self, key_id: str, envelope: dict) -> bytes:
        """解封收到的 DK 并缓存,之后 domain_key(key_id) 可用。"""
        dk = open_sealed(envelope, self.encryption_priv)
        self._keys[key_id] = dk
        return dk

    # ----- KeyProvider Protocol ---------------------------------------------- #

    def domain_key(self, key_id: str) -> bytes:
        if key_id not in self._keys:
            raise KeyError(f"no domain key for key_id={key_id!r}")
        return self._keys[key_id]
