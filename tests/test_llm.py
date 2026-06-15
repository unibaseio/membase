"""P0-7 测试:LLM 可插拔 + 输出校验。纯逻辑,无网络/无 SDK 依赖。"""

import os
import pytest

from membase.memory.llm import (
    build_summarizer, parse_summary_json,
    AnthropicSummarizer, OpenAISummarizer,
    DEFAULT_ANTHROPIC_MODEL,
)


# ----- 后端选择 ------------------------------------------------------------- #

def test_default_provider_is_anthropic(monkeypatch):
    monkeypatch.delenv("MEMBASE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MEMBASE_LLM_MODEL", raising=False)
    s = build_summarizer()
    assert isinstance(s, AnthropicSummarizer)
    assert s.model == DEFAULT_ANTHROPIC_MODEL  # 默认 Claude opus-4-8


def test_openai_provider_selected(monkeypatch):
    monkeypatch.setenv("MEMBASE_LLM_PROVIDER", "openai")
    assert isinstance(build_summarizer(), OpenAISummarizer)


def test_model_override(monkeypatch):
    monkeypatch.setenv("MEMBASE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("MEMBASE_LLM_MODEL", "claude-haiku-4-5")
    assert build_summarizer().model == "claude-haiku-4-5"


def test_construct_does_not_need_sdk_or_key():
    # 惰性:构造不触发 SDK 导入或 key 校验(不再 import 时 exit(1))
    AnthropicSummarizer()
    OpenAISummarizer()


# ----- 输出校验 ------------------------------------------------------------- #

def test_parse_plain_json():
    assert parse_summary_json('{"summary": "x"}') == {"summary": "x"}


def test_parse_strips_code_fence():
    text = '```json\n{"summary": "hi", "keywords": ["a"]}\n```'
    assert parse_summary_json(text) == {"summary": "hi", "keywords": ["a"]}


def test_parse_invalid_json_returns_none():
    assert parse_summary_json("not json at all") is None
    assert parse_summary_json("{broken: ") is None
    assert parse_summary_json("") is None
    assert parse_summary_json(None) is None


def test_parse_non_dict_returns_none():
    assert parse_summary_json("[1,2,3]") is None
    assert parse_summary_json('"a string"') is None


def test_parse_required_keys():
    assert parse_summary_json('{"summary":"x"}', required_keys=("summary",)) == {"summary": "x"}
    assert parse_summary_json('{"other":"x"}', required_keys=("summary",)) is None


# ----- summarize 方法健壮性(注入 fake summarizer) ------------------------- #

class FakeSummarizer:
    def __init__(self, out): self.out = out
    def complete(self, prompt, *, max_tokens=2048):
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


def _ltm(summarizer):
    """构造一个最小 LTMemory,只测 summarize 方法,绕开后台线程/hub/sqlite。"""
    from membase.memory.lt_memory import LTMemory
    obj = LTMemory.__new__(LTMemory)        # 不跑 __init__(避免 hub/sqlite/线程)
    obj._summarizer = summarizer
    obj._membase_account = "acct"
    return obj


def test_ltm_valid_output():
    obj = _ltm(FakeSummarizer('{"summary":"一段总结","keywords":["k"]}'))
    msg = obj.llm_summarize_ltm([], None)
    assert msg is not None and msg.type == "ltm"
    import json
    assert json.loads(msg.content)["summary"] == "一段总结"


def test_ltm_dirty_output_returns_none():
    obj = _ltm(FakeSummarizer("sorry I can't help"))   # 非 JSON
    assert obj.llm_summarize_ltm([], None) is None      # 不崩、不存脏数据


def test_ltm_missing_required_key_returns_none():
    obj = _ltm(FakeSummarizer('{"keywords":["k"]}'))    # 缺 summary
    assert obj.llm_summarize_ltm([], None) is None


def test_ltm_llm_exception_returns_none():
    obj = _ltm(FakeSummarizer(RuntimeError("API down")))
    assert obj.llm_summarize_ltm([], None) is None      # LLM 挂了也不崩线程


def test_profile_valid_and_dirty():
    ok = _ltm(FakeSummarizer('{"profile_summary":"画像"}'))
    assert ok.llm_summarize_profile([], None).type == "profile"
    bad = _ltm(FakeSummarizer("```\nnope\n```"))
    assert bad.llm_summarize_profile([], None) is None
