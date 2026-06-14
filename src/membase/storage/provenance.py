"""ProvenanceLog — 每 domain 一条 append-only 签名哈希链(D4 / 设计文档 §6)。

定位:DA 证明**存储层**完整性("这段字节没被改、还在线");provenance log 证明
**应用层**的顺序/归属/订正("这些事件按什么顺序、被谁写、谁订正了谁")。两者一拼
才是完整的"可验证记忆"。本类是纯 Python、不依赖 DA,可独立落地与测试。

链结构(§6.2):每条 LogEntry 含
  seq          单调递增(0 起)
  prev_hash    = 上一条的 entry_hash(genesis 为 "")
  op           PUT | SEAL | SUPERSEDE | GRANT | REVOKE
  payload      操作载荷(dict)
  author_addr  作者地址
  ts           unix 秒
  author_sig   对 canonical(上述内容字段) 的 EIP-191 签名
entry_hash = sha256(canonical(完整条目, 含 sig))。下一条 prev_hash 引用它。

防篡改:
  - author_sig 覆盖 prev_hash ⇒ 条目被绑定到链上具体位置,不能replay到别处。
  - 改任一历史条目 → 其 entry_hash 变 → 后续所有 prev_hash 链接断裂 + 该条 sig 失效。

⚠️ canonical 序列化必须**字节确定**(sorted keys + 无空格 + UTF-8),否则跨端
   重算 hash/验签会漂移(参考 DA 侧 Fiat-Shamir 的 byte-identity 教训)。
⚠️ schema 冻结要求:本结构若进入跨仓库线格式(§6.4 / CLAUDE.md 同步红线),
   需同步 JS/MCP。当前 D4 决策为"先 Python 内部",故暂不外扩。
"""

from __future__ import annotations

import os
import json
import time
import hashlib
import threading
from typing import Optional, Protocol, runtime_checkable, Iterator

import logging
logger = logging.getLogger(__name__)

GENESIS_PREV = ""
VALID_OPS = {"PUT", "SEAL", "SUPERSEDE", "GRANT", "REVOKE"}

# 内容字段(参与签名);author_sig / entry_hash 不在内。
_CONTENT_FIELDS = ("seq", "prev_hash", "op", "payload", "author_addr", "ts")


def _canonical(obj: dict) -> str:
    """字节确定的 JSON 序列化:键排序、无多余空格、不转义非 ASCII。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Signer / Verifier seam —— 复用 membase EIP-191,但解耦以便测试注入
# --------------------------------------------------------------------------- #

@runtime_checkable
class Signer(Protocol):
    @property
    def address(self) -> str: ...
    def sign_message(self, message: str) -> str: ...


@runtime_checkable
class Verifier(Protocol):
    def valid_signature(self, message: str, signature: str, address: str) -> bool: ...


class _MembaseChainSigner:
    """默认 Signer+Verifier:惰性包装 membase_chain(避免 import 时强制要求链 env)。"""

    def __init__(self):
        self._chain = None

    @property
    def _c(self):
        if self._chain is None:
            from membase.chain.chain import membase_chain  # 惰性:缺 env 会退出进程
            self._chain = membase_chain
        return self._chain

    @property
    def address(self) -> str:
        return self._c.wallet_address

    def sign_message(self, message: str) -> str:
        return self._c.sign_message(message)

    def valid_signature(self, message: str, signature: str, address: str) -> bool:
        return self._c.valid_signature(message, signature, address)


# --------------------------------------------------------------------------- #
# ProvenanceLog
# --------------------------------------------------------------------------- #

class ProvenanceLog:
    """单 domain 的签名哈希链,持久化为 append-only JSONL。

    满足 da_backend.ProvenanceLog Protocol:append(op, payload, author_addr) / head()。
    """

    def __init__(
        self,
        domain_id: str,
        *,
        signer: Optional[Signer] = None,
        verifier: Optional[Verifier] = None,
        path: Optional[str] = None,
        account: Optional[str] = None,
        clock=time.time,
    ):
        self.domain_id = domain_id
        self._clock = clock

        # 默认 signer/verifier 复用 membase_chain(惰性,不在此触发链 env)。
        default = _MembaseChainSigner()
        self._signer: Signer = signer or default
        self._verifier: Verifier = verifier or (default if signer is None else _RequireVerifier())

        if path is None:
            acct = account or os.getenv("MEMBASE_ACCOUNT", "default")
            base = os.path.join(os.path.expanduser("~"), ".membase", acct, "provenance")
            os.makedirs(base, exist_ok=True)
            path = os.path.join(base, f"{_safe(domain_id)}.jsonl")
        self.path = path

        self._lock = threading.Lock()
        self._head: Optional[str] = None  # 末条 entry_hash;空链为 None
        self._next_seq: int = 0
        self._load_tail()

    # ----- 公开 API ---------------------------------------------------------- #

    def append(self, *, op: str, payload: dict, author_addr: Optional[str] = None) -> str:
        """追加一条事件,返回新条目的 entry_hash。"""
        if op not in VALID_OPS:
            raise ValueError(f"unknown op {op!r}, must be one of {sorted(VALID_OPS)}")

        author_addr = author_addr or self._signer.address
        if author_addr != self._signer.address:
            raise ValueError(
                f"author_addr {author_addr} != signer {self._signer.address}; "
                "只能用持私钥的地址签名"
            )

        with self._lock:
            content = {
                "seq": self._next_seq,
                "prev_hash": self._head if self._head is not None else GENESIS_PREV,
                "op": op,
                "payload": payload,
                "author_addr": author_addr,
                "ts": int(self._clock()),
            }
            sig = self._signer.sign_message(_canonical(content))
            entry = dict(content)
            entry["author_sig"] = sig
            entry_hash = _sha256_hex(_canonical(entry))

            line = _canonical({**entry, "entry_hash": entry_hash})
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

            self._head = entry_hash
            self._next_seq += 1
            return entry_hash

    def head(self) -> Optional[str]:
        """当前链头 entry_hash(可用于上链锚定 / AIP 声誉指纹)。空链返回 None。"""
        return self._head

    def __len__(self) -> int:
        return self._next_seq

    def entries(self) -> Iterator[dict]:
        """按顺序产出所有条目(含 entry_hash)。"""
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def verify_chain(self) -> tuple[bool, Optional[str]]:
        """全量校验:seq 连续、prev_hash 链接、entry_hash 一致、每条 sig 有效。

        返回 (True, None) 或 (False, 失败原因)。
        """
        prev = GENESIS_PREV
        expected_seq = 0
        for entry in self.entries():
            seq = entry.get("seq")
            if seq != expected_seq:
                return False, f"seq gap at {seq}, expected {expected_seq}"
            if entry.get("prev_hash") != prev:
                return False, f"prev_hash mismatch at seq {seq}"

            stored_hash = entry.get("entry_hash")
            sig = entry.get("author_sig")
            content = {k: entry[k] for k in _CONTENT_FIELDS}
            # entry_hash 一致性(对 content+sig 重算)
            recomputed = _sha256_hex(_canonical({**content, "author_sig": sig}))
            if recomputed != stored_hash:
                return False, f"entry_hash mismatch at seq {seq}"
            # 签名有效性
            if not self._verifier.valid_signature(_canonical(content), sig, entry["author_addr"]):
                return False, f"invalid signature at seq {seq}"

            prev = stored_hash
            expected_seq += 1
        return True, None

    # ----- 内部 -------------------------------------------------------------- #

    def _load_tail(self):
        """从已有文件恢复 head 与 next_seq(不做全量验签,启动要快)。"""
        if not os.path.exists(self.path):
            return
        last = None
        count = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = line
                    count += 1
        if last:
            entry = json.loads(last)
            self._head = entry.get("entry_hash")
            self._next_seq = entry.get("seq", count - 1) + 1


class _RequireVerifier:
    """注入了自定义 signer 但未给 verifier 时的占位:验签需显式提供 verifier。"""
    def valid_signature(self, message: str, signature: str, address: str) -> bool:
        raise NotImplementedError(
            "注入了自定义 signer 时,verify_chain 需要同时注入 verifier"
        )


def _safe(name: str) -> str:
    """domain_id 转安全文件名。"""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "default"
