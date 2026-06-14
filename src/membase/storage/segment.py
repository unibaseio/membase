"""SegmentBuffer — 写路径编排:实现 §8.1 三步握手的 Python 侧(设计文档 §3 / §8.1)。

冷热双层(§3.2):消息先加密推送到热层 → 攒够阈值封段 → 封段进 DA(冷层,可验证)。
本模块只负责 **客户端能掌控的那部分**:加密(D2)、攒段、封段触发、provenance 编排
(D4)、manifest 组装。三个外部协作做成 seam(均有内存 fake 便于端到端测试):

  HotTier        热层:推送/读回密文(对应 DA hub logfs,§4)
  SealService    封段:把一段已推送的密文拼接 -> Upload -> 回传 da_cid + piece_core(D1,Go 侧)
  PieceRegistrar AddPiece 客户端自签上链(D3,见 da_backend)

§8.1 握手:
  put(msg):  encrypt(chunk) -> HotTier.push(密文) -> provenance.append(PUT)
  seal():    SealService.seal() -> da_cid -> Registrar.sign_and_submit_add_piece()
             -> 组装 SegmentManifest -> provenance.append(SEAL, da_cid=...)

hub 全程只见密文,密钥与签名始终在客户端。
"""

from __future__ import annotations

import os
import time
import uuid
import json
from typing import Optional, Protocol, runtime_checkable

from membase.storage.provenance import _canonical, _sha256_hex

import logging
logger = logging.getLogger(__name__)


def conversation_of(msg_id: str) -> str:
    """从 hub key 规则 {conversation_id}_{index} 反推 conversation_id。"""
    base, _, idx = msg_id.rpartition("_")
    return base if (base and idx.isdigit()) else msg_id


# --------------------------------------------------------------------------- #
# seam
# --------------------------------------------------------------------------- #

@runtime_checkable
class HotTier(Protocol):
    def push(self, *, owner: str, segment_id: str, chunk_idx: int, cipher_bytes: bytes) -> None: ...
    def read(self, *, owner: str, segment_id: str, cipher_offset: int, cipher_len: int) -> bytes: ...


@runtime_checkable
class SealService(Protocol):
    def seal(self, *, owner: str, segment_id: str, policy: tuple) -> dict:
        """拼接该段已推送密文 -> Upload(纠删) -> 返回 {'da_cid':..., 'piece_core':{...}}。"""
        ...


@runtime_checkable
class ColdTier(Protocol):
    """冷层读:从 DA 按 da_cid 取回(范围)密文(对应 sdk.Download / 纠删重建,§7.1)。"""
    def read(self, *, da_cid: str, cipher_offset: int, cipher_len: int) -> bytes: ...


@runtime_checkable
class PieceRegistrar(Protocol):
    def sign_and_submit_add_piece(self, piece_core: dict) -> str: ...


# --------------------------------------------------------------------------- #
# 内存 fake(端到端测试用;生产替换为 DA hub HTTP / 链上实现)
# --------------------------------------------------------------------------- #

class InMemoryHotTier:
    def __init__(self):
        self._buf: dict = {}  # (owner, segment_id) -> bytearray(顺序拼接密文)

    def push(self, *, owner, segment_id, chunk_idx, cipher_bytes):
        self._buf.setdefault((owner, segment_id), bytearray()).extend(cipher_bytes)

    def read(self, *, owner, segment_id, cipher_offset, cipher_len):
        b = self._buf.get((owner, segment_id), b"")
        return bytes(b[cipher_offset: cipher_offset + cipher_len])

    def raw(self, owner, segment_id) -> bytes:
        return bytes(self._buf.get((owner, segment_id), b""))


class InMemorySealService:
    """fake 封段:da_cid = sha256(整段密文)(占位真实 KZG 承诺);保留密文供读回。"""

    def __init__(self, hot: InMemoryHotTier):
        self._hot = hot
        self.sealed: dict = {}  # da_cid -> ciphertext

    def seal(self, *, owner, segment_id, policy):
        import hashlib
        ct = self._hot.raw(owner, segment_id)
        da_cid = "cid_" + hashlib.sha256(ct).hexdigest()
        self.sealed[da_cid] = ct
        return {
            "da_cid": da_cid,
            "piece_core": {"name": da_cid, "size": len(ct),
                           "policy": list(policy), "streamer": "0xstream"},
        }

    # 兼作 ColdTier:封段后按 da_cid 取回(范围)密文
    def read(self, *, da_cid, cipher_offset, cipher_len):
        ct = self.sealed.get(da_cid, b"")
        return bytes(ct[cipher_offset: cipher_offset + cipher_len])


class FakeRegistrar:
    def __init__(self):
        self.calls: list = []

    def sign_and_submit_add_piece(self, piece_core: dict) -> str:
        self.calls.append(piece_core)
        return "0xtx_" + piece_core["name"][:12]


# --------------------------------------------------------------------------- #
# SegmentBuffer
# --------------------------------------------------------------------------- #

class SegmentBuffer:
    """单 domain 的写路径编排器。线程不安全调用方需自行加锁(DAHubBackend 持锁)。"""

    def __init__(
        self,
        *,
        owner: str,
        domain_id: str,
        encryptor,                 # storage.encryptor.Encryptor
        provenance,                # storage.provenance.ProvenanceLog
        hot: HotTier,
        sealer: SealService,
        registrar: PieceRegistrar,
        cold: Optional[ColdTier] = None,  # 冷层读;默认复用 sealer(若实现了 read)
        key_id: str = "dk-v1",
        policy: tuple = (6, 4),    # D5
        seal_size: int = 1 << 20,  # §3.2 阈值:≥1MB
        seal_count: int = 1024,    # 或 ≥1024 条
        seal_interval: float = 3600.0,  # 或距上次封段 ≥1h
        clock=time.time,
        segment_id_factory=None,
        manifests_path: Optional[str] = None,  # 持久化 manifest(durable 读索引来源)
    ):
        self.owner = owner
        self.domain_id = domain_id
        self._enc = encryptor
        self._log = provenance
        self._hot = hot
        self._sealer = sealer
        self._registrar = registrar
        self._cold = cold or (sealer if hasattr(sealer, "read") else None)
        self.key_id = key_id
        self.policy = policy
        self.seal_size = seal_size
        self.seal_count = seal_count
        self.seal_interval = seal_interval
        self._clock = clock
        self._seg_id_factory = segment_id_factory or (lambda: uuid.uuid4().hex)

        self.manifests_path = manifests_path
        self.sealed_manifests: list = []  # 封过的段 manifest(读路径索引来源)
        self._load_manifests()
        self._open = None
        self._open_segment()

    # ----- 写 ---------------------------------------------------------------- #

    def put(self, msg_bytes: bytes, *, msg_id: str, mtype: str = "stm",
            conversation_id: Optional[str] = None) -> dict:
        """加密一条消息 -> 推热层 -> 写 PUT。达阈值自动封段。返回本条定位信息。"""
        seg = self._open
        conv = conversation_id or conversation_of(msg_id)
        cipher, cm = seg["writer"].add(msg_bytes)
        self._hot.push(owner=self.owner, segment_id=seg["id"],
                       chunk_idx=cm.idx, cipher_bytes=cipher)
        entry = {"msg_id": msg_id, "conversation_id": conv, "type": mtype, "chunk_idx": cm.idx,
                 "plain_offset": cm.plain_offset, "plain_len": cm.plain_len,
                 "cipher_offset": cm.cipher_offset, "cipher_len": cm.cipher_len}
        seg["index"].append(entry)
        self._log.append(op="PUT", payload={"segment_id": seg["id"], **entry})

        if self._should_seal(seg):
            self.seal()
        return {"segment_id": seg["id"], **entry}

    def supersede(self, old_msg_id: str, *, reason: str = "") -> str:
        """订正:写 SUPERSEDE 事件(§3.3 / §7.3)。旧段不变,读视图重建时跳过被订正项。"""
        return self._log.append(op="SUPERSEDE",
                                payload={"target": old_msg_id, "reason": reason})

    def seal(self) -> Optional[dict]:
        """封当前段:Upload -> 自签 AddPiece -> 组装 manifest -> 写 SEAL。空段不封。"""
        seg = self._open
        if not seg["index"]:
            return None

        res = self._sealer.seal(owner=self.owner, segment_id=seg["id"], policy=self.policy)
        da_cid = res["da_cid"]
        tx = self._registrar.sign_and_submit_add_piece(res["piece_core"])

        w = seg["writer"]
        msg_range = {"start_index": seg["index"][0]["chunk_idx"],
                     "end_index": seg["index"][-1]["chunk_idx"],
                     "count": len(seg["index"])}
        manifest = {
            "segment_id": seg["id"],
            "domain_id": self.domain_id,
            "da_cid": da_cid,
            "policy": list(self.policy),
            "enc": w.manifest_enc(),
            "size_plain": w.size_plain,
            "size_cipher": w.size_cipher,
            "msg_range": msg_range,
            "msg_index": seg["index"],
            "sealed_at": int(self._clock()),
            "add_piece_tx": tx,
        }
        # manifest_hash 进 SEAL 事件 ⇒ 签名哈希链锚定 manifest,读时可验真(§6 / §7)
        manifest_hash = _sha256_hex(_canonical(manifest))
        manifest["manifest_hash"] = manifest_hash
        self.sealed_manifests.append(manifest)
        self._persist_manifest(manifest)
        self._log.append(op="SEAL", payload={
            "segment_id": seg["id"], "da_cid": da_cid,
            "msg_range": msg_range, "add_piece_tx": tx, "manifest_hash": manifest_hash,
        })
        logger.info("sealed segment %s -> %s (%d msgs, tx %s)",
                    seg["id"], da_cid, msg_range["count"], tx)
        self._open_segment()
        return manifest

    def flush(self) -> Optional[dict]:
        """强制封当前段(高风险时刻 / 优雅退出)。"""
        return self.seal()

    def open_manifest(self) -> Optional[dict]:
        """在途(未封)段的 manifest 视图(da_cid=None ⇒ 读走热层)。供读路径含最新数据。"""
        seg = self._open
        if not seg["index"]:
            return None
        w = seg["writer"]
        return {
            "segment_id": seg["id"], "domain_id": self.domain_id, "da_cid": None,
            "enc": w.manifest_enc(), "size_plain": w.size_plain, "size_cipher": w.size_cipher,
            "msg_index": list(seg["index"]),
            "msg_range": {"start_index": seg["index"][0]["chunk_idx"],
                          "end_index": seg["index"][-1]["chunk_idx"], "count": len(seg["index"])},
        }

    def all_manifests(self) -> list:
        """封段 + 在途段(若非空),供读路径取全量视图(含最新数据)。"""
        m = list(self.sealed_manifests)
        om = self.open_manifest()
        if om:
            m.append(om)
        return m

    # ----- 读(段级,供读路径 §7.1 复用) ------------------------------------ #

    def read_message(self, manifest: dict, msg_id: str) -> bytes:
        """读回单条消息明文:定位 chunk -> 取密文(已封走冷层 da_cid,在途走热层)-> 解密。"""
        entry = next((e for e in manifest["msg_index"] if e["msg_id"] == msg_id), None)
        if entry is None:
            raise KeyError(msg_id)
        da_cid = manifest.get("da_cid")
        if da_cid:  # 冷层:DA 按 da_cid 取回(§7.1 cold)
            cipher = self._cold.read(da_cid=da_cid, cipher_offset=entry["cipher_offset"],
                                     cipher_len=entry["cipher_len"])
        else:       # 在途段:热层快路径(§7.1 fast)
            cipher = self._hot.read(owner=self.owner, segment_id=manifest["segment_id"],
                                    cipher_offset=entry["cipher_offset"],
                                    cipher_len=entry["cipher_len"])
        chunk_meta = {"idx": entry["chunk_idx"]}
        return self._enc.decrypt_chunk(manifest["enc"], chunk_meta, cipher,
                                       key_id=manifest["enc"]["key_id"],
                                       segment_id=manifest["segment_id"])

    # ----- 内部 -------------------------------------------------------------- #

    def _persist_manifest(self, manifest: dict):
        if not self.manifests_path:
            return
        os.makedirs(os.path.dirname(self.manifests_path), exist_ok=True)
        with open(self.manifests_path, "a", encoding="utf-8") as f:
            f.write(_canonical(manifest) + "\n")

    def _load_manifests(self):
        if not self.manifests_path or not os.path.exists(self.manifests_path):
            return
        with open(self.manifests_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    self.sealed_manifests.append(json.loads(line))

    def _open_segment(self):
        sid = self._seg_id_factory()
        self._open = {
            "id": sid,
            "writer": self._enc.segment_writer(key_id=self.key_id, segment_id=sid),
            "index": [],
            "started_at": self._clock(),
        }

    def _should_seal(self, seg) -> bool:
        w = seg["writer"]
        return (w.size_plain >= self.seal_size
                or len(seg["index"]) >= self.seal_count
                or (self._clock() - seg["started_at"]) >= self.seal_interval)
