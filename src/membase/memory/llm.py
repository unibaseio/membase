"""LLM 抽象(P0-7):可插拔总结后端 + 输出校验。

lt_memory 原硬绑 OpenAI 且 import 时缺 key 直接 exit(1),LLM 返回脏 JSON 会崩后台线程。
本模块把 LLM 调用抽象为 Summarizer seam(默认 Claude),并提供健壮的 JSON 校验。

选择(env):
  MEMBASE_LLM_PROVIDER = anthropic(默认) | openai
  MEMBASE_LLM_MODEL    = 覆盖模型;默认 anthropic=claude-opus-4-8,openai=gpt-4.1-mini

SDK 惰性导入(在 complete() 内),故无 key/未装 SDK 也能 import 本模块与构造对象,
测试可注入 fake summarizer。
"""

from __future__ import annotations

import os
import json
from typing import Optional, Protocol, runtime_checkable

import logging
logger = logging.getLogger(__name__)

DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"


@runtime_checkable
class Summarizer(Protocol):
    def complete(self, prompt: str, *, max_tokens: int = 2048) -> str:
        """返回模型文本输出(供 parse_summary_json 校验)。"""
        ...


class AnthropicSummarizer:
    """默认后端:Claude。总结为高频后台任务,默认不开 thinking(快/省),输出走文本。"""

    def __init__(self, model: Optional[str] = None):
        self.model = model or os.getenv("MEMBASE_LLM_MODEL", DEFAULT_ANTHROPIC_MODEL)
        self._client = None

    def _c(self):
        if self._client is None:
            import anthropic  # 惰性
            self._client = anthropic.Anthropic()  # 读 ANTHROPIC_API_KEY
        return self._client

    def complete(self, prompt: str, *, max_tokens: int = 2048) -> str:
        resp = self._c().messages.create(
            model=self.model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )  # 注:opus-4-8 不接受 temperature(已移除)
        return "".join(b.text for b in resp.content if b.type == "text")


class OpenAISummarizer:
    """兼容后端:保留原 OpenAI 行为。"""

    def __init__(self, model: Optional[str] = None):
        self.model = model or os.getenv("MEMBASE_LLM_MODEL", DEFAULT_OPENAI_MODEL)
        self._client = None

    def _c(self):
        if self._client is None:
            from openai import OpenAI  # 惰性
            self._client = OpenAI()  # 读 OPENAI_API_KEY
        return self._client

    def complete(self, prompt: str, *, max_tokens: int = 2048) -> str:
        resp = self._c().chat.completions.create(
            model=self.model, messages=[{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=max_tokens,
        )
        return resp.choices[0].message.content


def build_summarizer() -> Summarizer:
    provider = os.getenv("MEMBASE_LLM_PROVIDER", "anthropic").lower()
    if provider == "openai":
        logger.info("LTMemory summarizer: OpenAI (%s)", os.getenv("MEMBASE_LLM_MODEL", DEFAULT_OPENAI_MODEL))
        return OpenAISummarizer()
    logger.info("LTMemory summarizer: Anthropic (%s)", os.getenv("MEMBASE_LLM_MODEL", DEFAULT_ANTHROPIC_MODEL))
    return AnthropicSummarizer()


def parse_summary_json(text: str, *, required_keys=None) -> Optional[dict]:
    """健壮校验 LLM 输出:剥 ```json 围栏 -> json.loads -> 必须是含 required_keys 的 dict。

    任何不合法返回 None(调用方据此跳过,不崩溃、不存脏数据)。
    """
    if not text:
        return None
    s = text.strip()
    if "```" in s:
        s = s.replace("```json", "").replace("```", "").strip()
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    if required_keys and not all(k in obj for k in required_keys):
        return None
    return obj
