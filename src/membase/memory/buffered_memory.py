# -*- coding: utf-8 -*-
"""
Memory module for conversation
"""

import json
import logging
import os
import uuid
from typing import Iterable, Sequence, Optional, Union, Callable

from loguru import logger

from .memory import MemoryBase
from .serialize import serialize, deserialize
from .message import Message

from membase.storage.hub import hub_client

class BufferedMemory(MemoryBase):
    """
    In-memory memory module, not writing to hard disk
    """

    def __init__(
        self,
        conversation_id: Optional[str] = None,
        membase_account: str = "default",
        auto_upload_to_hub: bool = False
    ) -> None:
        """
        Buffered memory module for conversation.
        """
        super().__init__()

        self._messages = []
        self._message_map = {} 

        # conversation_id is none or empty, generate a new uuid
        if not conversation_id:
            self._conversation_id = str(uuid.uuid4())
        else:
            self._conversation_id = conversation_id

        self._membase_account = membase_account
        self._auto_upload_to_hub = auto_upload_to_hub
    
    def add(
        self,
        memories: Union[Sequence[Message], Message, None],
    ) -> None:
        """
        Adding new memory fragment, depending on how the memory are stored
        """
        self.add_with_upload(memories, True)

    def add_with_upload(
        self,
        memories: Union[Sequence[Message], Message, None],
        upload_to_hub: bool = True,
    ) -> None:
        """
        Adding new memory fragment, depending on how the memory are stored
        Args:
            memories (`Union[Sequence[Message], Message, None]`):
                Memories to be added.
        """
        if memories is None:
            return

        if not isinstance(memories, Sequence):
            record_memories = [memories]
        else:
            record_memories = memories

        # Assert the message types and check for duplicates using dict
        for memory_unit in record_memories:
            if not isinstance(memory_unit, Message):
                raise ValueError(
                    f"Cannot add {type(memory_unit)} to memory, "
                    f"must be a Message object.",
                )
            
            # Skip if message already exists
            if hasattr(memory_unit, "id") and memory_unit.id in self._message_map:
                logging.warn("duplicate memory_unit:", memory_unit.id)
                continue

            # Add metadata
            if isinstance(memory_unit.metadata, dict):
                memory_unit.metadata["conversation"] = self._conversation_id
            elif isinstance(memory_unit.metadata, str):
                memory_unit.metadata = {'metadata': memory_unit.metadata, 'conversation': self._conversation_id}
            else:
                memory_unit.metadata = {'conversation': self._conversation_id}
            
            # Add to memory and update map
            self._messages.append(memory_unit)
            self._message_map[memory_unit.id] = len(self._messages) - 1

            # Upload to hub if needed
            if self._auto_upload_to_hub and upload_to_hub:
                msg = serialize(memory_unit)
                memory_id = self._conversation_id + "_" + str(len(self._messages)-1)
                logging.debug(f"Upload memory: {self._membase_account} {memory_id}")
                hub_client.upload_hub(self._membase_account, memory_id, msg)

    def delete(self, index: Union[Iterable, int]) -> None:
        """
        Delete memory fragment, depending on how the memory are stored
        and matched
        Args:
            index (Union[Iterable, int]):
                indices of the memory fragments to delete
        """
        if self.size() == 0:
            logger.warning(
                "The memory is empty, and the delete operation is "
                "skipping.",
            )
            return

        if isinstance(index, int):
            index = [index]

        if isinstance(index, list):
            index = set(index)

            invalid_index = [_ for _ in index if _ >= self.size() or _ < 0]
            if len(invalid_index) > 0:
                logger.warning(
                    f"Skip delete operation for the invalid "
                    f"index {invalid_index}",
                )

            # Update message map before deleting messages
            new_messages = []
            new_message_map = {}
            for i, msg in enumerate(self._messages):
                if i not in index:
                    new_messages.append(msg)
                    if hasattr(msg, "id"):
                        new_message_map[msg.id] = len(new_messages) - 1

            self._messages = new_messages
            self._message_map = new_message_map
        else:
            raise NotImplementedError(
                "index type only supports {None, int, list}",
            )

    def get(
        self,
        recent_n: Optional[int] = None,
        filter_func: Optional[Callable[[int, dict], bool]] = None,
    ) -> list:
        """Retrieve memory.

        Args:
            recent_n (`Optional[int]`, default `None`):
                The last number of memories to return.
            filter_func
                (`Callable[[int, dict], bool]`, default to `None`):
                The function to filter memories, which take the index and
                memory unit as input, and return a boolean value.
        """
        # extract the recent `recent_n` entries in memories
        if recent_n is None:
            memories = self._messages
        else:
            if recent_n > self.size():
                recent_n = self.size()
            memories = self._messages[-recent_n:]

        # filter the memories
        if filter_func is not None:
            memories = [_ for i, _ in enumerate(memories) if filter_func(i, _)]

        return memories

    def export(
        self,
        file_path: Optional[str] = None,
        to_mem: bool = False,
    ) -> Optional[list]:
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
        if to_mem:
            return self._messages

        if to_mem is False and file_path is not None:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(serialize(self._messages))
        else:
            raise NotImplementedError(
                "file type only supports "
                "{json, yaml, pkl}, default is json",
            )
        return None

    def load(
        self,
        memories: Union[str, list[Message], Message],
        overwrite: bool = False,
    ) -> None:
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
        if isinstance(memories, str):
            if os.path.isfile(memories):
                with open(memories, "r", encoding="utf-8") as f:
                    load_memories = deserialize(f.read())
            else:
                try:
                    load_memories = deserialize(memories)
                    if not isinstance(load_memories, dict) and not isinstance(
                        load_memories,
                        list,
                    ):
                        logger.warning(
                            "The memory loaded by json.loads is "
                            "neither a dict nor a list, which may "
                            "cause unpredictable errors.",
                        )
                except json.JSONDecodeError as e:
                    raise json.JSONDecodeError(
                        f"Cannot load [{memories}] via " f"json.loads.",
                        e.doc,
                        e.pos,
                    )
        elif isinstance(memories, list):
            for unit in memories:
                if not isinstance(unit, Message):
                    raise TypeError(
                        f"Expect a list of Message objects, but get {type(unit)} "
                        f"instead.",
                    )
            load_memories = memories
        elif isinstance(memories, Message):
            load_memories = [memories]
        else:
            raise TypeError(
                f"The type of memories to be loaded is not supported. "
                f"Expect str, list[Message], or Message, but get {type(memories)}.",
            )

        # overwrite the original memories after loading the new ones
        if overwrite:
            self.clear()
            if len(load_memories) > 0 and 'conversation' in load_memories[0].metadata:
                self._conversation_id = load_memories[0].metadata['conversation']
                membase_account = os.getenv('MEMBASE_ACCOUNT')
                if membase_account and membase_account != "":
                    self._owner = membase_account
                else: 
                    self._owner = self._conversation_id 

        self.add_with_upload(load_memories, False)

    def clear(self) -> None:
        """Clean memory, depending on how the memory are stored"""
        self._messages = []
        self._message_map = {}
        self._conversation_id = str(uuid.uuid4())
        membase_account = os.getenv('MEMBASE_ACCOUNT')
        if membase_account and membase_account != "":
            self._owner = membase_account
        else: 
            self._owner = self._conversation_id 

    def size(self) -> int:
        """Returns the number of memory segments in memory."""
        return len(self._messages)


    
