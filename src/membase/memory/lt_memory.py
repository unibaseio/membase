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
import threading
import time
import hashlib
import openai

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
        self._profile_conversation_id = self._get_profile_conversation_id()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._background_task, daemon=True)
        self._thread.start()
            
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

    def _get_profile_conversation_id(self):
        # 每个account一个profile conversation
        key = self._membase_account + "_profile"
        return hashlib.sha256(key.encode()).hexdigest()

    def _background_task(self):
        stms_per_ltm = 16  # 每次归纳的stm数量
        while not self._stop_event.is_set():
            for conv_id in self.get_all_conversations():
                memory = self.get_memory(conv_id)
                # 获取最新stm的memory_index
                latest_stm_list = memory.get(recent_n=1, type='stm')
                if latest_stm_list and latest_stm_list[0].metadata and 'memory_index' in latest_stm_list[0].metadata:
                    latest_stm_index = latest_stm_list[0].metadata['memory_index']
                else:
                    latest_stm_index = -1
                # 获取最新ltm的memory_index
                prev_ltm_list = memory.get(recent_n=1, type='ltm')
                if prev_ltm_list and prev_ltm_list[0].metadata and 'memory_index' in prev_ltm_list[0].metadata:
                    last_ltm_index = prev_ltm_list[0].metadata['memory_index']
                else:
                    last_ltm_index = -1

                # 判断是否有足够多的新stm需要归纳，如果不需要，则等待60秒
                # 进行一次归纳
                if latest_stm_index // stms_per_ltm > (last_ltm_index+1):
                    # 计算本轮要归纳的stm的index范围
                    start_index = (last_ltm_index + 1) * stms_per_ltm
                    end_index = start_index + stms_per_ltm - 1
                    # 取出memory_index在[start_index, end_index]之间的stm
                    stm_list = memory.get(type='stm', filter_func=lambda idx, msg: msg.metadata and start_index <= msg.metadata.get('memory_index', -1) <= end_index)
                    if len(stm_list) < stms_per_ltm:
                        continue  # 理论上不会发生，保险起见
                    prev_ltm = prev_ltm_list[0] if prev_ltm_list else None
                    new_ltm = self.llm_summarize_ltm(stm_list, prev_ltm)
                    if not hasattr(new_ltm, 'metadata') or new_ltm.metadata is None:
                        new_ltm.metadata = {}
                    self.add(new_ltm, conversation_id=conv_id)
                    # profile归纳
                    prev_profile_list = self.get_memory(self._profile_conversation_id).get(recent_n=1, type='profile')
                    prev_profile = prev_profile_list[0] if prev_profile_list else None
                    new_profile = self.llm_summarize_profile(new_ltm, prev_profile)
                    self.add(new_profile, conversation_id=self._profile_conversation_id)
                time.sleep(60)
        time.sleep(60)

    def llm_summarize_ltm(self, stm_list, prev_ltm):
        # 使用OpenAI gpt-4.1-mini生成新ltm，格式后续可自定义
        prompt = self._build_ltm_prompt(stm_list, prev_ltm)
        try:
            response = openai.ChatCompletion.create(
                model="gpt-4.1-mini",  # gpt-4.1-mini
                messages=[{"role": "user", "content": prompt}],
                max_tokens=512
            )
            content = response["choices"][0]["message"]["content"]
            return Message(name=self._membase_account, content=content, role="user", type="ltm")
        except Exception as e:
            logging.error(f"Error summarizing ltm: {e}")
            return None

    def llm_summarize_profile(self, new_ltm, prev_profile):
        # 使用OpenAI gpt-4.1-mini生成新profile，格式后续可自定义
        prompt = self._build_profile_prompt(new_ltm, prev_profile)
        response = openai.ChatCompletion.create(
            model="gpt-4.1-mini",  # gpt-4.1-mini
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512
        )
        content = response["choices"][0]["message"]["content"]
        return Message(name=self._membase_account, content=content, role="user", type="profile")

    def _build_ltm_prompt(self, stm_list, prev_ltm):
        import json
        # 结构化stm为JSON数组
        stm_json_list = [
            {
                "content": m.content,
                "role": getattr(m, "role", ""),
                "name": getattr(m, "name", ""),
                "timestamp": getattr(m, "timestamp", "")
            }
            for m in stm_list
        ]
        stm_text = json.dumps(stm_json_list, ensure_ascii=False, indent=2)
        prev = prev_ltm.content if prev_ltm else "无"
        return (
            "你是一个智能体的长期记忆归纳助手，目标是将多轮对话内容转化为对后续智能体推理、检索、用户建模最有价值的长期记忆（ltm）。\n"
            "【长期记忆的用途包括但不限于】：\n"
            "1. 支持智能体后续推理和决策\n"
            "2. 作为知识库、经验库、用户画像的输入\n"
            "3. 支持检索、标签化、快速定位关键信息\n"
            f"【前一轮长期记忆】\n{prev}\n"
            f"【本轮对话内容】（JSON数组，每条为一条消息对象）:\n{stm_text}\n"
            "请严格遵循以下要求进行归纳：\n"
            "1. 重点提炼对后续推理、知识积累、用户理解有价值的事实、规则、偏好、意图、知识点，去除无关、重复、琐碎内容。\n"
            "2. 如有多条重要信息，请分条列出，便于后续检索和调用。\n"
            "3. 为本轮对话归纳3-8个高相关标签（keywords），便于后续索引。\n"
            "4. 进行结构化分析（analysis），请分别从以下五个维度详细分析本轮对话的长期记忆价值：\n"
            "   - semantic_complexity：语义深度与隐含信息（如复杂推理、隐含动机、潜台词等）\n"
            "   - long_term_value：长期价值（对未来决策、持续服务的参考意义）\n"
            "   - profile_relevance：用户特征关联（是否反映用户习惯、偏好、身份、目标等）\n"
            "   - emotion_intent：情感与意图（体现的情绪、态度或明确/隐含的意图）\n"
            "   - knowledge_potential：知识与经验（是否包含新知识、经验总结、方法论等）\n"
            "5. 根据分析结果，为本轮对话分配记忆等级（1-5级，定义如下）：\n"
            "   1-丢弃：无信息价值，如闲聊、确认、寒暄\n"
            "   2-情景：具体事件、时间、地点、任务安排\n"
            "   3-偏好：用户习惯、兴趣、风格、价值观\n"
            "   4-经验：流程、方法、经验总结、操作步骤\n"
            "   5-知识：抽象概念、原理、深度洞察、战略思考\n"
            "6. 严格以如下JSON格式输出，所有字段必须完整且类型正确，summary为字符串且不少于100字，keywords为字符串数组，memory_level为1-5的整数，analysis为结构化对象：\n"
            "{\n"
            "  \"summary\": \"归纳总结（不少于100字，分条列出更佳）\",\n"
            "  \"keywords\": [\"标签1\", \"标签2\", ...],\n"
            "  \"memory_level\": 1,\n"
            "  \"analysis\": {\n"
            "    \"semantic_complexity\": \"...\",\n"
            "    \"long_term_value\": \"...\",\n"
            "    \"profile_relevance\": \"...\",\n"
            "    \"emotion_intent\": \"...\",\n"
            "    \"knowledge_potential\": \"...\"\n"
            "  }\n"
            "}\n"
            "请严格校验输出 JSON 格式，确保可被 json.loads 正确解析，否则会被判为无效结果。\n"
            "除JSON外不要输出任何解释说明。"
        )

    def _build_profile_prompt(self, stm_list, prev_profile):
        stm_json_list = [
            {
                "content": m.content,
                "role": getattr(m, "role", ""),
                "name": getattr(m, "name", ""),
                "timestamp": getattr(m, "timestamp", "")
            }
            for m in stm_list
        ]
        stm_text = json.dumps(stm_json_list, ensure_ascii=False, indent=2)
        prev = prev_profile.content if prev_profile else "无"
        return (
            "你是一个用户画像分析助手，目标是通过分析用户最近的对话内容和历史画像，持续学习和动态更新用户profile，使智能体（LLM）后续的回复风格、内容、表达方式等更加贴合用户的真实习惯和个性化需求。\n"
            f"【前一个用户画像】\n{prev}\n"
            f"【最新对话内容】（JSON数组，每条为一条消息对象）:\n{stm_text}\n"
            "请严格按照以下要求进行分析和输出：\n"
            "1. 结合历史画像和最新对话内容，归纳用户的：\n"
            "   - 语言风格（如正式/口语、偏好中英文、表达习惯、常用语气等）\n"
            "   - 知识水平与专业领域（如是否具备专业背景、常用术语、表达深度等）\n"
            "   - 兴趣领域（如AI、编程、艺术等）\n"
            "   - 偏好（如喜欢的功能、常用操作、内容风格、信息呈现方式等）\n"
            "   - 行为模式（如提问方式、决策习惯、互动频率、主动/被动等）\n"
            "   - 目标与动机（如追求效率、创新、学习、娱乐等）\n"
            "2. 归纳3-8个高相关标签（tags），便于后续检索和个性化推荐。\n"
            "3. 对比新旧画像，明确指出本轮对话带来的主要变化（main_changes）。\n"
            "4. profile_summary 要求分条、具体，且不少于100字，突出对LLM后续输出风格和内容的指导建议。\n"
            "5. 严格以如下JSON格式输出，所有字段必须完整且类型正确：\n"
            "{\n"
            "  \"profile_summary\": \"用户画像更新总结（不少于100字，分条列出更佳，突出对LLM输出的指导建议）\",\n"
            "  \"language_style\": \"...\",\n"
            "  \"knowledge_level\": \"...\",\n"
            "  \"interests\": [\"兴趣1\", \"兴趣2\", ...],\n"
            "  \"preferences\": [\"偏好1\", \"偏好2\", ...],\n"
            "  \"behavior_patterns\": [\"模式1\", \"模式2\", ...],\n"
            "  \"goals_motivations\": [\"目标/动机1\", ...],\n"
            "  \"tags\": [\"标签1\", \"标签2\", ...],\n"
            "  \"main_changes\": \"主要变化点\"\n"
            "}\n"
            "请严格校验输出 JSON 格式，确保可被 json.loads 正确解析，否则会被判为无效结果。\n"
            "除JSON外不要输出任何解释说明。"
        )

    def stop(self):
        self._stop_event.set()
        self._thread.join()
