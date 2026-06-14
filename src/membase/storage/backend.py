"""HubBackend — membase 记忆存储后端的抽象边界(adapter boundary)。

membase 的所有调用方(buffered/multi/sqlite memory、knowledge/chroma、lt_memory)
只依赖这组方法。任何后端实现满足本接口即可热插拔:

  - Client          (storage/hub.py)         —— legacy 中心化 hub(现状)
  - DAHubBackend    (storage/da_backend.py)  —— P1 可验证存储,坐到 Unibase DA 之上

选择由 env `MEMBASE_HUB_BACKEND={legacy|da}` 决定(见 storage/hub.py 工厂)。

设计依据:../MEMBASE_DA_INTEGRATION_DESIGN.md(§4 hub 收敛到 DA hub)。
⚠️ 这组签名对应 `membase/CLAUDE.md` 的"Hub REST API"同步红线 —— 改方法签名/语义
   需同步 Python/JS/plugin 三份客户端。本接口只是把现有 Client 的隐式契约显式化,
   未改变任何线格式或 endpoint 语义。
"""

from abc import ABC, abstractmethod
from typing import Optional


class HubBackend(ABC):
    """记忆存储后端契约。方法签名与现有 storage/hub.py::Client 完全一致。"""

    @abstractmethod
    def upload_hub(self, owner, filename, msg, bucket: Optional[str] = None, wait: bool = True):
        """上传一条记忆(JSON 线格式字符串)。后台队列处理,wait=True 阻塞至完成。

        返回:wait=True -> 完成状态 dict;wait=False -> 入队状态 dict;失败 -> None。
        """
        raise NotImplementedError

    @abstractmethod
    def upload_hub_data(self, owner, filename, data):
        """上传二进制 blob(multipart)。返回响应 dict 或 None。"""
        raise NotImplementedError

    @abstractmethod
    def list_conversations(self, owner):
        """列出 owner 的全部会话。返回 list 或 None。"""
        raise NotImplementedError

    @abstractmethod
    def get_conversation(self, owner, conversation_id):
        """取某会话全部消息。返回 list 或 None。"""
        raise NotImplementedError

    @abstractmethod
    def download_hub(self, owner, filename):
        """下载一条记忆/blob,返回 bytes 或 None。"""
        raise NotImplementedError

    @abstractmethod
    def wait_for_upload_queue(self):
        """阻塞直到上传队列清空(用于优雅退出 / 测试)。"""
        raise NotImplementedError
