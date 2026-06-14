"""Reader — DA 读路径(设计文档 §7)。

从 manifest 集合(durable 读索引)重建可读视图:
  - get_message(msg_id):   定位 chunk -> 取密文(冷层 da_cid / 热层在途)-> 解密
  - get_conversation(cid):  按 conversation 归集 + 全局顺序 + 应用 SUPERSEDE(§7.3)
                            -> 返回有序的序列化 Message JSON 串(与 legacy hub 契约一致)

顺序:段按封段先后、段内按 chunk_idx,即全局写入序。SUPERSEDE 集合从 provenance log
扫得(§7.3),被订正的 msg_id 在视图重建时跳过。

验证(§7.1):日常读**不做** CheckFileFull(贵);信任 = 网络持续 epoch 证明 + 可选
校验 manifest_hash 是否与 provenance SEAL 事件锚定值一致(verify=True 时)。
"""

from __future__ import annotations

from typing import Optional

import logging
logger = logging.getLogger(__name__)


class Reader:
    def __init__(self, manifests: list, *, segment_reader, provenance=None):
        """
        manifests:     SegmentBuffer.all_manifests()(封段 + 在途段)
        segment_reader: 暴露 read_message(manifest, msg_id) 的对象(SegmentBuffer)
        provenance:    可选,用于扫 SUPERSEDE 事件与校验 manifest_hash 锚定
        """
        self._manifests = manifests
        self._sr = segment_reader
        self._prov = provenance

        # msg_id -> manifest;conversation_id -> 有序 msg_id 列表(段序 + chunk_idx)
        self._by_msg: dict = {}
        self._by_conv: dict = {}
        for man in manifests:
            for e in man["msg_index"]:
                self._by_msg[e["msg_id"]] = man
                self._by_conv.setdefault(e.get("conversation_id", ""), []).append(
                    (e["chunk_idx"], e["msg_id"], man["segment_id"]))

        self._superseded = self._scan_superseded()

    # ----- 公开读 ------------------------------------------------------------ #

    def get_message(self, msg_id: str) -> Optional[bytes]:
        man = self._by_msg.get(msg_id)
        if man is None:
            return None
        return self._sr.read_message(man, msg_id)

    def get_conversation(self, conversation_id: str) -> list:
        """返回有序、去订正后的序列化 Message JSON 串列表(可直接喂 legacy 重建逻辑)。"""
        items = self._by_conv.get(conversation_id, [])
        # 段序(manifests 已是封段先后)+ 段内 chunk_idx ⇒ 用 manifest 出现序作主键
        seg_order = {m["segment_id"]: i for i, m in enumerate(self._manifests)}
        items = sorted(items, key=lambda t: (seg_order.get(t[2], 0), t[0]))

        out = []
        for _, msg_id, _ in items:
            if msg_id in self._superseded:
                continue  # §7.3 跳过被订正项
            data = self.get_message(msg_id)
            if data is not None:
                out.append(data.decode("utf-8"))
        return out

    def conversations(self) -> list:
        return [c for c in self._by_conv.keys() if c]

    # ----- 校验(可选) ------------------------------------------------------ #

    def verify_anchors(self) -> tuple:
        """校验每个封段 manifest_hash 与 provenance SEAL 锚定一致(§7 信任检查)。"""
        if self._prov is None:
            return False, "no provenance log to verify against"
        sealed = {e["payload"]["segment_id"]: e["payload"].get("manifest_hash")
                  for e in self._prov.entries() if e["op"] == "SEAL"}
        for man in self._manifests:
            if man.get("da_cid") is None:
                continue  # 在途段未上链,不校验
            if sealed.get(man["segment_id"]) != man.get("manifest_hash"):
                return False, f"manifest_hash mismatch for segment {man['segment_id']}"
        return True, None

    # ----- 内部 -------------------------------------------------------------- #

    def _scan_superseded(self) -> set:
        s = set()
        if self._prov is None:
            return s
        for e in self._prov.entries():
            if e["op"] == "SUPERSEDE":
                t = e["payload"].get("target")
                if isinstance(t, str):
                    s.add(t)
        return s
