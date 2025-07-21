# -*- coding: utf-8 -*-
"""
SqliteMemory: 使用sqlite3持久化存储memory
"""
import os
import sqlite3
import json
from typing import Optional, Union, Sequence, Callable, List
import uuid
from .message import Message
from .serialize import serialize
from membase.knowledge.chroma import ChromaKnowledgeBase
from membase.knowledge.document import Document
from membase.storage.hub import hub_client

import logging

class SqliteMemory:
    def __init__(self, conversation_id: Optional[str], membase_account: str = "default", auto_upload_to_hub: bool = False, knowledge_base=None):
        if not conversation_id:
            self.conversation_id = str(uuid.uuid4())
        else:
            self.conversation_id = conversation_id
        self.membase_account = membase_account
        self.auto_upload_to_hub = auto_upload_to_hub
        self.db_path = os.path.expanduser(f"~/.membase/{membase_account}/sql.db")
        self._ensure_db()
        if knowledge_base is not None:
            self.knowledge_base = knowledge_base
        else:
            self.knowledge_base = ChromaKnowledgeBase(
                persist_directory=f"~/.membase/{membase_account}/rag",
                collection_name=membase_account + "_memory",
                membase_account=membase_account
            )

    def _ensure_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        # 创建stm表
        c.execute('''
            CREATE TABLE IF NOT EXISTS memories_stm (
                id TEXT PRIMARY KEY,
                conversation_id TEXT,
                content TEXT,
                memory_index INTEGER,
                upload_status INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # 创建ltm表
        c.execute('''
            CREATE TABLE IF NOT EXISTS memories_ltm (
                id TEXT PRIMARY KEY,
                conversation_id TEXT,
                content TEXT,
                memory_index INTEGER,
                upload_status INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # 创建profile表
        c.execute('''
            CREATE TABLE IF NOT EXISTS memories_profile (
                id TEXT PRIMARY KEY,
                conversation_id TEXT,
                content TEXT,
                memory_index INTEGER,
                upload_status INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()

    def add(self, memories: Union[Sequence[Message], Message, None], from_hub: bool = False):
        if memories is None:
            return
        if not isinstance(memories, Sequence) or isinstance(memories, str):
            memories = [memories]
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        hub_memories = {}
        for msg in memories:
            if not isinstance(msg, Message):
                raise ValueError(f"Cannot add {type(msg)} to memory, must be a Message object.")
            msg_dict = msg.to_dict()
            # conversation_id作为metadata字段
            if isinstance(msg_dict.get("metadata"), dict):
                msg_dict["metadata"]["conversation"] = self.conversation_id
            else:
                msg_dict["metadata"] = {"conversation": self.conversation_id}
            # 选择表名
            if msg.type == "ltm":
                table = "memories_ltm"
                msg_dict["memory_type"] = "ltm"
            elif msg.type == "profile":
                table = "memories_profile"
                msg_dict["memory_type"] = "profile"
            else:
                table = "memories_stm"
                msg_dict["memory_type"] = "stm"
            # 获取当前memory_index
            c.execute(f'SELECT MAX(memory_index) FROM {table} WHERE conversation_id = ?', (self.conversation_id,))
            row = c.fetchone()
            memory_index = (row[0] + 1) if row[0] is not None else 0
            msg_dict["metadata"]["memory_index"] = memory_index
            # 插入sqlite
            c.execute(f'''
                INSERT OR REPLACE INTO {table} (id, conversation_id, content, memory_index, upload_status) VALUES (?, ?, ?, ?, 0)
            ''', (msg.id, self.conversation_id, json.dumps(msg_dict, ensure_ascii=False), memory_index))
            # 检查RAG是否已存在
            if not self.knowledge_base.exists(msg.id):
                doc = Document(
                    content=msg.content,
                    metadata=msg_dict["metadata"],
                    doc_id=msg.id
                )
                self.knowledge_base.add_documents(doc)
            # save to hub_memories
            msg_id = self.conversation_id + "_" + str(memory_index)
            if msg.type == "ltm":
                msg_id = "ltm_" + msg_id
            elif msg.type == "profile":
                msg_id = "profile_" + msg_id
            hub_memories[msg_id] = (msg_dict, memory_index, msg.type)
        conn.commit()
        conn.close()
        
        # 单独循环处理上传到hub
        if self.auto_upload_to_hub and not from_hub:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            for msg_id, (msg_dict, memory_index, msg_type) in hub_memories.items():
                msg_serialized = serialize(msg_dict)
                logging.debug(f"Upload memory: {self.membase_account} {msg_id}")
                hub_client.upload_hub(self.membase_account, msg_id, msg_serialized)
                if msg_type == "ltm":
                    table = "memories_ltm"
                elif msg_type == "profile":
                    table = "memories_profile"
                else:
                    table = "memories_stm"
                c.execute(f'UPDATE {table} SET upload_status=1 WHERE conversation_id=? AND memory_index=?', (self.conversation_id, memory_index))
            conn.commit()
            conn.close()

    def get(self, recent_n: Optional[int] = None, filter_func: Optional[Callable[[int, dict], bool]] = None, type: str = "stm") -> List[Message]:
        if type == "ltm":
            table = "memories_ltm"
        elif type == "profile":
            table = "memories_profile"
        else:
            table = "memories_stm"
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        sql = f"SELECT content FROM {table} WHERE conversation_id = ? ORDER BY created_at DESC"
        params = [self.conversation_id]
        if recent_n is not None:
            sql += " LIMIT ?"
            params.append(recent_n)
        c.execute(sql, params)
        rows = c.fetchall()
        rows.reverse()
        conn.close()
        messages = [Message.from_dict(json.loads(row[0])) for row in rows]
        if filter_func is not None:
            messages = [msg for i, msg in enumerate(messages) if filter_func(i, msg)]
        return messages

    def delete(self, memory_indexes: Union[List[int], int], type: str = "stm"):
        if isinstance(memory_indexes, int):
            memory_indexes = [memory_indexes]
        if type == "ltm":
            table = "memories_ltm"
        elif type == "profile":
            table = "memories_profile"
        else:
            table = "memories_stm"
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.executemany(
            f"DELETE FROM {table} WHERE conversation_id = ? AND memory_index = ?",
            [(self.conversation_id, idx) for idx in memory_indexes]
        )
        conn.commit()
        conn.close()

    def clear(self, type: Optional[str] = None):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        if type is None:
            c.execute("DELETE FROM memories_stm WHERE conversation_id = ?", (self.conversation_id,))
            c.execute("DELETE FROM memories_ltm WHERE conversation_id = ?", (self.conversation_id,))
            c.execute("DELETE FROM memories_profile WHERE conversation_id = ?", (self.conversation_id,))
        else:
            if type == "ltm":
                table = "memories_ltm"
            elif type == "profile":
                table = "memories_profile"
            else:
                table = "memories_stm"
            c.execute(f"DELETE FROM {table} WHERE conversation_id = ?", (self.conversation_id,))
        conn.commit()
        conn.close()

    def size(self, type: str = "stm") -> int:
        if type == "ltm":
            table = "memories_ltm"
        elif type == "profile":
            table = "memories_profile"
        else:
            table = "memories_stm"
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(f"SELECT COUNT(*) FROM {table} WHERE conversation_id = ?", (self.conversation_id,))
        count = c.fetchone()[0]
        conn.close()
        return count

    def load(self, memories: Union[str, list[Message], Message], overwrite: bool = False) -> None:
        """
        Load memory, depending on how the memory are passed, design to load
        from both file or dict
        Args:
            memories (Union[str, list[Message], Message]):
                memories to be loaded.
                If it is in str type, it will be first checked if it is a
                file; otherwise it will be deserialized as messages.
                Otherwise, memories must be either in message type or list
                 of messages.
            overwrite (bool):
                if True, clear the current memory before loading the new ones;
                if False, memories will be appended to the old one at the end.
        """
        import os
        from .serialize import deserialize
        if isinstance(memories, str):
            if os.path.isfile(memories):
                with open(memories, "r", encoding="utf-8") as f:
                    load_memories = deserialize(f.read())
            else:
                try:
                    load_memories = deserialize(memories)
                except Exception as e:
                    raise ValueError(f"Cannot load [{memories}] via json.loads: {e}")
        elif isinstance(memories, list):
            load_memories = memories
        elif isinstance(memories, Message):
            load_memories = [memories]
        else:
            raise TypeError(f"The type of memories to be loaded is not supported. Expect str, list[Message], or Message, but get {type(memories)}.")
        if overwrite:
            self.clear()
        self.add(load_memories)

    def export(self, file_path: Optional[str] = None, to_mem: bool = False) -> Optional[list]:
        """
        Export memory, depending on how the memory are stored
        Args:
            file_path (Optional[str]):
                file path to save the memory to. The messages will
                be serialized and written to the file.
            to_mem (Optional[str]):
                if True, just return the list of messages in memory
        Notice: this method prevents file_path is None when to_mem
        is False.
        """
        from .serialize import serialize
        messages = self.get()
        if to_mem:
            return messages
        if file_path is not None:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(serialize(messages))
        else:
            raise NotImplementedError("file type only supports {json, yaml, pkl}, default is json")
        return None 