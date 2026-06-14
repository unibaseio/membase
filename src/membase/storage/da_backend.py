"""DAHubBackend — P1 可验证存储后端(**接口骨架,未实现 DA 逻辑**)。

把 membase 记忆坐到 Unibase DA(去中心化存储 + Onchain ZK 验证网络)之上。
完整设计见 ../MEMBASE_DA_INTEGRATION_DESIGN.md。

锁定决策(§8):
  D1  封段在 DA hub(Go)侧 —— 本类只推送密文 + 触发封段,纠删/Upload 在 Go 侧
  D2  加密在客户端(本类)  —— 推送前 AEAD 分块加密,hub/store 全程不见明文(§5)
  D3  AddPiece 客户端自签  —— 复用 membase_chain 的 account 本地签名+广播(§8.1)
  D4  provenance log 先 Python 内部 —— 本地 append-only 签名哈希链(§6)
  D5  默认纠删 (6,4)

D1+D2+D3 的握手(§8.1,实现时必须照此,否则 hub 会接触明文或密钥):
  1. 客户端按 chunk 边界 AEAD 加密 -> 推送【密文】到 DA hub 热层(logfs)
  2. DA hub 达阈值 -> 拼接密文成 segment -> Upload -> 回传 piece CID + PieceCore
  3. 客户端用自己私钥签 AddPiece(CID) -> 本地广播 -> 写 SEAL 事件到 provenance log

⚠️ 当前状态:公开方法签名与 HubBackend 对齐(adapter 可切换),但所有 DA 相关
   私有步骤 raise NotImplementedError。把 env MEMBASE_HUB_BACKEND=da 打开会立刻
   在写路径报清晰的 "not implemented" 错误,而不是静默走错行为。

依赖(注入式协作者,均为待实现 seam):
  - Encryptor       客户端信封加密(§5)
  - ProvenanceLog   签名哈希链(§6)
  - PieceRegistrar  客户端自签 AddPiece(§3.2 / D3)
  - DA hub HTTP     热层推送 + 封段触发(§4)
"""

from __future__ import annotations

import os
import logging
import threading
from typing import Optional, Protocol, runtime_checkable

from membase.storage.backend import HubBackend

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 协作者 seam(Protocol)—— 仅定义边界,实现留到 P1 各子任务
# --------------------------------------------------------------------------- #

@runtime_checkable
class Encryptor(Protocol):
    """客户端信封加密(§5)。domain key 派生 + 分块 AEAD。

    实现见 storage/encryptor.py::Encryptor。返回的 EncryptedSegment 带 .manifest()
    供 §3.4 segment manifest 使用;读单条走 decrypt_chunk()。
    """

    def encrypt(self, plaintext: bytes, *, key_id: str, segment_id: str): ...
    def decrypt(self, seg) -> bytes: ...


@runtime_checkable
class ProvenanceLog(Protocol):
    """每 domain 一条 append-only 签名哈希链(§6)。"""

    def append(self, *, op: str, payload: dict, author_addr: str) -> str:
        """追加一条 LogEntry(自动填 seq/prev_hash,作者签名),返回 entry_hash。"""
        ...

    def head(self) -> Optional[str]:
        """当前链头 hash(用于上链锚定 / AIP 声誉指纹)。"""
        ...


@runtime_checkable
class PieceRegistrar(Protocol):
    """客户端自签 AddPiece 上链(D3 / §3.2)。复用 membase_chain account。"""

    def sign_and_submit_add_piece(self, piece_core: dict) -> str:
        """用客户端私钥签 AddPiece 交易并广播(或签后经 hub 中继),返回 tx hash。"""
        ...


# --------------------------------------------------------------------------- #
# DAHubBackend 骨架
# --------------------------------------------------------------------------- #

class DAHubBackend(HubBackend):
    """坐在 Unibase DA 之上的记忆存储后端(骨架)。"""

    def __init__(
        self,
        base_url: str,
        *,
        encryptor: Optional[Encryptor] = None,
        provenance: Optional[ProvenanceLog] = None,
        registrar: Optional[PieceRegistrar] = None,
        hot=None,                 # segment.HotTier
        sealer=None,              # segment.SealService
        domain_id: Optional[str] = None,
        key_id: str = "dk-v1",
        policy: tuple = (6, 4),   # D5
    ):
        self.base_url = base_url  # DA hub HTTP endpoint(env MEMBASE_HUB)
        self.membase_id = os.getenv("MEMBASE_ID", "")
        self.domain_id = domain_id or self.membase_id or "default"
        self.key_id = key_id
        self.policy = policy

        # 协作者:P1 各子任务实现后注入;缺失则写路径使用时清晰报错。
        self._enc = encryptor
        self._log = provenance
        self._registrar = registrar
        self._hot = hot
        self._sealer = sealer

        self._lock = threading.Lock()
        self._buffer = None  # 惰性构建 SegmentBuffer(需全部写路径 seam 就位)

        if not self._write_ready():
            logger.warning(
                "DAHubBackend 写路径 seam 未全部注入(encryptor/provenance/registrar/"
                "hot/sealer),upload_hub 调用时将报错。读路径见 §7(待实现)。"
            )

    def _write_ready(self) -> bool:
        return all(x is not None for x in
                   (self._enc, self._log, self._registrar, self._hot, self._sealer))

    def _get_buffer(self):
        if not self._write_ready():
            missing = [n for n, x in (("encryptor", self._enc), ("provenance", self._log),
                                      ("registrar", self._registrar), ("hot", self._hot),
                                      ("sealer", self._sealer)) if x is None]
            raise NotImplementedError(
                f"DAHubBackend 写路径 seam 缺失:{missing}。"
                "注入后即走 §8.1 握手(加密->热层->封段->自签 AddPiece->SEAL)。"
            )
        if self._buffer is None:
            from membase.storage.segment import SegmentBuffer
            self._buffer = SegmentBuffer(
                owner=self.domain_id, domain_id=self.domain_id,
                encryptor=self._enc, provenance=self._log,
                hot=self._hot, sealer=self._sealer, registrar=self._registrar,
                key_id=self.key_id, policy=self.policy,
            )
        return self._buffer

    # ----- HubBackend 公开接口(adapter 边界,签名与 Client 一致) ----------- #

    def upload_hub(self, owner, filename, msg, bucket: Optional[str] = None, wait: bool = True):
        """§8.1 写路径:加密 -> 推热层 -> 写 PUT(达阈值自动封段 -> 自签 AddPiece -> SEAL)。

        filename 即 msg_id;msg 为 JSON 线格式字符串(与 legacy Client 一致)。
        """
        buf = self._get_buffer()
        data = msg.encode("utf-8") if isinstance(msg, str) else bytes(msg)
        with self._lock:
            loc = buf.put(data, msg_id=filename)
        return {"status": "completed", "segment_id": loc["segment_id"],
                "chunk_idx": loc["chunk_idx"]}

    def upload_hub_data(self, owner, filename, data):
        # 大 blob(知识库/向量段):作为一条消息进缓冲,通常配合显式 flush 立即封段
        buf = self._get_buffer()
        with self._lock:
            loc = buf.put(bytes(data), msg_id=filename, mtype="blob")
        return {"status": "completed", "segment_id": loc["segment_id"]}

    def flush(self):
        """强制封当前段(高风险时刻 / 优雅退出)。"""
        if self._buffer is None:
            return None
        with self._lock:
            return self._buffer.flush()

    def _reader(self):
        from membase.storage.reader import Reader
        buf = self._get_buffer()
        return Reader(buf.all_manifests(), segment_reader=buf, provenance=self._log)

    def list_conversations(self, owner):
        """读路径(§7):列出 manifest 索引里的全部 conversation。"""
        with self._lock:
            return self._reader().conversations()

    def get_conversation(self, owner, conversation_id):
        """读路径(§7):按 conversation 归集 -> 取密文(冷/热)-> 解密 -> 应用 SUPERSEDE。

        返回有序的序列化 Message JSON 串列表,与 legacy hub 契约一致。
        """
        with self._lock:
            return self._reader().get_conversation(conversation_id)

    def download_hub(self, owner, filename):
        """读路径(§7.1):按 msg_id 读回单条明文 bytes(冷层 Download / 热层快路径 -> 解密)。"""
        with self._lock:
            return self._reader().get_message(filename)

    def wait_for_upload_queue(self):
        # 写路径同步落盘/推送;封段为显式触发。优雅退出走 flush()。
        return None

    # 注:§8.1 三步握手的编排已下沉到 storage/segment.py::SegmentBuffer,
    #     DAHubBackend 仅做 HubBackend 适配 + seam 注入 + 加锁。
