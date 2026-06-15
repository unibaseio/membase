"""EciesKeyProvider 测试(D2 真 domain-key 投递)。ECIES 纯密码学,无链/无网络。"""

import pytest

from membase.storage.keyring import (
    EciesKeyProvider, seal, open_sealed,
    generate_encryption_key, encryption_public_key,
)
from membase.storage.encryptor import Encryptor, KeyProvider


# ----- ECIES 密封盒 --------------------------------------------------------- #

def test_seal_open_roundtrip():
    priv = generate_encryption_key()
    pub = encryption_public_key(priv)
    env = seal(b"secret domain key", pub)
    assert open_sealed(env, priv) == b"secret domain key"
    # envelope 里看不到明文
    assert "secret" not in (env["ct"] + env["epk"] + env["nonce"])


def test_open_wrong_key_fails():
    env = seal(b"dk", encryption_public_key(generate_encryption_key()))
    with pytest.raises(ValueError):
        open_sealed(env, generate_encryption_key())   # 别人的私钥


def test_open_tampered_fails():
    priv = generate_encryption_key()
    env = seal(b"dk", encryption_public_key(priv))
    bad = dict(env, ct=("ff" + env["ct"][2:]))         # 翻转密文首字节
    with pytest.raises(ValueError):
        open_sealed(bad, priv)


def test_seal_is_nondeterministic():
    priv = generate_encryption_key(); pub = encryption_public_key(priv)
    a, b = seal(b"x", pub), seal(b"x", pub)
    assert a["epk"] != b["epk"] and a["ct"] != b["ct"]  # 每次临时 keypair


# ----- 授权 = 交付 DK ------------------------------------------------------- #

def test_grant_accept_delivers_domain_key():
    owner = EciesKeyProvider()
    grantee = EciesKeyProvider()
    dk = owner.create_domain_key("dk-v1")

    # owner 用 grantee 的加密公钥封装 DK(链上 buy 授权后投递)
    envelope = owner.grant("dk-v1", grantee.encryption_pub)
    # grantee 解封 -> 之后 domain_key 可用,且 == owner 的 DK
    assert grantee.accept("dk-v1", envelope) == dk
    assert grantee.domain_key("dk-v1") == dk


def test_other_agent_cannot_open_grant():
    owner = EciesKeyProvider(); grantee = EciesKeyProvider(); attacker = EciesKeyProvider()
    owner.create_domain_key("dk-v1")
    envelope = owner.grant("dk-v1", grantee.encryption_pub)
    with pytest.raises(ValueError):
        attacker.accept("dk-v1", envelope)             # 非被授权方解不开


def test_domain_key_missing_raises():
    with pytest.raises(KeyError):
        EciesKeyProvider().domain_key("nope")


def test_grant_unknown_key_raises():
    with pytest.raises(KeyError):
        EciesKeyProvider().grant("nope", EciesKeyProvider().encryption_pub)


def test_satisfies_keyprovider_protocol():
    kp = EciesKeyProvider(); kp.create_domain_key("dk-v1")
    assert isinstance(kp, KeyProvider)


# ----- 与 Encryptor 集成:授权后才能解密段(完整 D2 路径) ----------------- #

def test_grantee_can_decrypt_after_grant():
    owner = EciesKeyProvider(); grantee = EciesKeyProvider()
    owner.create_domain_key("dk-v1")

    # owner 用 DK 加密一段记忆
    blob = Encryptor(owner).encrypt(b"owned memory", key_id="dk-v1", segment_id="seg-1")

    # grantee 在拿到授权(grant->accept)前解不了(无 DK)
    grantee_enc = Encryptor(grantee)
    with pytest.raises(KeyError):
        grantee_enc.decrypt(blob)

    # 授权后(交付 DK)即可解
    grantee.accept("dk-v1", owner.grant("dk-v1", grantee.encryption_pub))
    assert grantee_enc.decrypt(blob) == b"owned memory"
