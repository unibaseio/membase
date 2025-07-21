# -*- coding: utf-8 -*-
"""
LTMemory module for managing multiple SqliteMemory instances
"""

import json
import logging
from typing import Optional, Dict, List, Union, Callable
import uuid
from .message import Message
from .sqlite_memory import SqliteMemory

from membase.storage.hub import hub_client

class LTMemory:
    """
    A class that manages multiple Memory instances, distinguished by conversation_id
    """
    
    def __init__(self, 
                 membase_account: str = "default", 
                 auto_upload_to_hub: bool = False, 
                 default_conversation_id: Optional[str] = None,
                 preload_from_hub: bool = False
                 ):
        """
        Initialize LTMemory

        Args:
            membase_account (str): The membase account name
            auto_upload_to_hub (bool): Whether to automatically upload to hub
            default_conversation_id (Optional[str]): The default conversation ID. If None, generates a new UUID.
            preload_from_hub (bool): Whether to preload from hub
        """
        self._memories: Dict[str, SqliteMemory] = {}
        self._membase_account = membase_account
        self._auto_upload_to_hub = auto_upload_to_hub
        self._default_conversation_id = default_conversation_id or str(uuid.uuid4())
        self._preload_conversations = {}
        if preload_from_hub:
            self.load_all_from_hub()
            
    def update_conversation_id(self, conversation_id: Optional[str] = None) -> None:
        """
        Update the default conversation ID. If conversation_id is None, generates a new UUID.

        Args:
            conversation_id (Optional[str]): The new default conversation ID. If None, generates a new UUID.
        """
        self._default_conversation_id = conversation_id or str(uuid.uuid4())
        
    def get_memory(self, conversation_id: Optional[str] = None) -> SqliteMemory:
        """
        Get SqliteMemory instance for the specified conversation_id.
        Creates a new instance if it doesn't exist.

        Args:
            conversation_id (Optional[str]): The conversation ID. If None, uses default ID.

        Returns:
            SqliteMemory: The corresponding memory instance
        """
        if not conversation_id:
            conversation_id = self._default_conversation_id
        if conversation_id not in self._memories:
            self._memories[conversation_id] = SqliteMemory(
                conversation_id=conversation_id,
                membase_account=self._membase_account,
                auto_upload_to_hub=self._auto_upload_to_hub
            )
        return self._memories[conversation_id]
    
    def add(self, memories: Union[List[Message], Message, None], conversation_id: Optional[str] = None) -> None:
        """
        Add memories to the specified conversation

        Args:
            memories: The memories to add
            conversation_id (Optional[str]): The conversation ID. If None, uses default ID.
        """
        memory = self.get_memory(conversation_id)
        memory.add(memories, from_hub=False)
        
    def get(self, conversation_id: Optional[str] = None, recent_n: Optional[int] = None,
            filter_func: Optional[Callable[[int, dict], bool]] = None) -> list:
        """
        Get memories from the specified conversation

        Args:
            conversation_id (Optional[str]): The conversation ID. If None, uses default ID.
            recent_n (Optional[int]): Number of recent memories to retrieve
            filter_func (Optional[Callable]): Filter function for memories

        Returns:
            list: List of memories
        """
        memory = self.get_memory(conversation_id)
        return memory.get(recent_n=recent_n, filter_func=filter_func)
        
    def delete(self, conversation_id: Optional[str] = None, index: Union[List[int], int] = None) -> None:
        """
        Delete memories from the specified conversation

        Args:
            conversation_id (Optional[str]): The conversation ID. If None, uses default ID.
            index: Index or indices of memories to delete
        """
        if conversation_id is None:
            conversation_id = self._default_conversation_id
            
        if conversation_id in self._memories:
            self._memories[conversation_id].delete(index)
            
    def clear(self, conversation_id: Optional[str] = None) -> None:
        """
        Clear memories from the specified conversation.
        If conversation_id is None, clears all memories.

        Args:
            conversation_id (Optional[str]): The conversation ID. If None, clears all conversations.
        """
        if conversation_id is None:
            self._memories.clear()
            self._default_conversation_id = str(uuid.uuid4())
        elif conversation_id in self._memories:
            self._memories[conversation_id].clear()
            
    def get_all_conversations(self) -> List[str]:
        """
        Get all conversation IDs

        Returns:
            List[str]: List of conversation IDs
        """
        return list(self._memories.keys())
    
    def size(self, conversation_id: Optional[str] = None) -> int:
        """
        Get the number of memories

        Args:
            conversation_id (Optional[str]): The conversation ID.
                If None, returns total count of memories across all conversations.

        Returns:
            int: Number of memories
        """
        if conversation_id is None:
            return sum(memory.size() for memory in self._memories.values())
        elif conversation_id in self._memories:
            return self._memories[conversation_id].size()
        return 0
        
    @property
    def default_conversation_id(self) -> str:
        """
        Get the default conversation ID

        Returns:
            str: The default conversation ID
        """
        return self._default_conversation_id

    def load_from_hub(self, conversation_id: str) -> None:
        """
        Load memories from hub for the specified conversation.

        Args:
            conversation_id (str): The conversation ID to load.
        """
        # check if the conversation has been preloaded
        if self.is_preloaded(conversation_id):
            return
        # Record the preloaded conversation
        self._preload_conversations[conversation_id] = True
        
        memory = self.get_memory(conversation_id)
        msgstrings = hub_client.get_conversation(self._membase_account, conversation_id)
        if msgstrings is None:
            return 
        for msgstring in msgstrings:
            try:
                logging.debug("got msg:", msgstring)
                json_msg = json.loads(msgstring)
                # check json_msg is a Message dict
                if isinstance(json_msg, dict) and "id" in json_msg and "name" in json_msg:
                    msg = Message.from_dict(json_msg)
                    memory.add(msg, from_hub=True)
                else:
                    logging.debug("invalid message format:", json_msg)
            except Exception as e:
                logging.error(f"Error loading message: {e}")
        
        
    def load_all_from_hub(self) -> None:
        """
        Load all memories from hub for all conversations under the current account.

        Args:
            overwrite (bool): Whether to overwrite existing memories
        """
        conversations = hub_client.list_conversations(self._membase_account)
        if conversations and isinstance(conversations, list):
            logging.info("remote conversations:", conversations)
            for conv_id in conversations:
                self.load_from_hub(conv_id)
        else:
            logging.warning("no conversations found")
            
    def is_preloaded(self, conversation_id: str) -> bool:
        """
        Check if a conversation has been preloaded from hub.

        Args:
            conversation_id (str): The conversation ID to check.

        Returns:
            bool: True if the conversation has been preloaded, False otherwise.
        """
        return conversation_id in self._preload_conversations
