"""Encryptor 测试 —— 信封加密 + 分块,纯客户端,无需链/网络。"""

import json
import pytest

from membase.storage.encryptor import (
    Encryptor, InMemoryKeyProvider, EncryptedSegment, ALG, TAG_LEN,
)


@pytest.fixture
def enc_small_chunks():
    kp = InMemoryKeyProvider()
    kp.generate("dk-v1")
    return Encryptor(kp, chunk_size=64), kp  # 小块,便于跨多块测试


def _roundtrip(enc, data, key_id="dk-v1", seg="seg-0"):
    blob = enc.encrypt(data, key_id=key_id, segment_id=seg)
    return blob, enc.decrypt(blob)


def test_roundtrip_basic(enc_small_chunks):
    enc, _ = enc_small_chunks
    data = b"hello membase verifiable memory" * 10
    blob, out = _roundtrip(enc, data)
    assert out == data
    assert blob.alg == ALG and blob.size_plain == len(data)
    assert len(blob.chunks) > 1  # 跨多块


def test_empty_plaintext(enc_small_chunks):
    enc, _ = enc_small_chunks
    blob, out = _roundtrip(enc, b"")
    assert out == b"" and len(blob.chunks) == 1


def test_ciphertext_not_plaintext(enc_small_chunks):
    enc, _ = enc_small_chunks
    data = b"SECRET-PROFILE-DATA" * 5
    blob, _ = _roundtrip(enc, data)
    # 明文片段不应出现在密文里(hub/store 看到的是 blob.ciphertext)
    assert b"SECRET" not in blob.ciphertext
    # 每块密文 = 明文长 + tag
    for c in blob.chunks:
        assert c.cipher_len == c.plain_len + TAG_LEN


def test_tamper_detected(enc_small_chunks):
    enc, _ = enc_small_chunks
    blob, _ = _roundtrip(enc, b"x" * 200)
    bad = bytearray(blob.ciphertext)
    bad[0] ^= 0xFF  # 翻转一位
    tampered = EncryptedSegment(**{**blob.__dict__, "ciphertext": bytes(bad)})
    with pytest.raises(ValueError):  # GCM tag 校验失败
        enc.decrypt(tampered)


def test_wrong_key_fails(enc_small_chunks):
    enc, kp = enc_small_chunks
    blob, _ = _roundtrip(enc, b"data" * 50)
    # 换一把 DK 解 → tag 失败
    kp2 = InMemoryKeyProvider(); kp2.generate("dk-v1")
    enc2 = Encryptor(kp2, chunk_size=64)
    with pytest.raises(ValueError):
        enc2.decrypt(blob)


def test_segment_id_isolation(enc_small_chunks):
    enc, _ = enc_small_chunks
    # 同 DK、不同 segment_id ⇒ SK 不同,密文不同(即便明文相同)
    b1 = enc.encrypt(b"same plaintext here", key_id="dk-v1", segment_id="seg-A")
    b2 = enc.encrypt(b"same plaintext here", key_id="dk-v1", segment_id="seg-B")
    assert b1.ciphertext != b2.ciphertext


def test_no_nonce_reuse_on_reencrypt(enc_small_chunks):
    enc, _ = enc_small_chunks
    # 同 segment_id 重复加密:base_nonce 随机 ⇒ 密文不同(规避 GCM nonce 复用)
    b1 = enc.encrypt(b"abc" * 40, key_id="dk-v1", segment_id="seg-X")
    b2 = enc.encrypt(b"abc" * 40, key_id="dk-v1", segment_id="seg-X")
    assert b1.base_nonce != b2.base_nonce
    assert b1.ciphertext != b2.ciphertext


def test_single_chunk_decrypt_via_manifest(enc_small_chunks):
    enc, _ = enc_small_chunks
    # 模拟段内多条消息;读单条只解相关 chunk
    data = bytes(range(256)) * 4  # 1024B,跨多个 64B 块
    blob = enc.encrypt(data, key_id="dk-v1", segment_id="seg-R")

    # manifest 可 JSON 化(进 §3.4 SegmentManifest)
    man = blob.manifest()
    man = json.loads(json.dumps(man))
    enc_meta, chunks = man["enc"], man["chunks"]

    # 想读明文区间 [100, 130)
    offset, length = 100, 30
    hit = Encryptor.chunks_for_range(chunks, offset, length)
    assert len(hit) >= 1

    # 只取这些 chunk 的密文(模拟从 DA 按 cipher_offset/cipher_len 取回),逐块解密
    recon = bytearray()
    base_plain = hit[0]["plain_offset"]
    for cm in hit:
        cb = blob.ciphertext[cm["cipher_offset"]: cm["cipher_offset"] + cm["cipher_len"]]
        recon.extend(enc.decrypt_chunk(enc_meta, cm, cb, key_id="dk-v1", segment_id="seg-R"))
    # 从拼回的块里切出目标区间
    sliced = bytes(recon)[offset - base_plain: offset - base_plain + length]
    assert sliced == data[offset:offset + length]


def test_manifest_has_no_secrets(enc_small_chunks):
    enc, _ = enc_small_chunks
    blob = enc.encrypt(b"top secret" * 20, key_id="dk-v1", segment_id="seg-M")
    man = blob.manifest()
    flat = json.dumps(man)
    assert "key_id" in man["enc"]          # 有密钥版本标识
    assert b"top secret".decode() not in flat  # 无明文
    # 无 DK / SK 本体
    assert "domain_key" not in flat
