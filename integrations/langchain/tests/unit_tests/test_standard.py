"""LangChain's own standard test suites (langchain-tests), run offline against the fake API."""

from __future__ import annotations

from typing import Any

from langchain_tests.integration_tests import RetrieversIntegrationTests, ToolsIntegrationTests
from langchain_tests.unit_tests import ToolsUnitTests

from langchain_membase import (
    MembaseProfileTool,
    MembaseRetriever,
    MembaseSaveTool,
    MembaseSearchTool,
)

from .conftest import FakeMembase

_FAKE = FakeMembase()


class TestMembaseRetrieverStandard(RetrieversIntegrationTests):
    @property
    def retriever_constructor(self) -> type[MembaseRetriever]:
        return MembaseRetriever

    @property
    def retriever_constructor_params(self) -> dict[str, Any]:
        return {"client": _FAKE.client()}

    @property
    def retriever_query_example(self) -> str:
        return "what did we decide about the ledger"


class _ToolParams:
    tool_class: type
    tool_name: str
    example: dict[str, Any]

    @property
    def tool_constructor(self) -> type:
        return self.tool_class

    @property
    def tool_constructor_params(self) -> dict[str, Any]:
        return {"client": _FAKE.client()}

    @property
    def tool_invoke_params_example(self) -> dict[str, Any]:
        return self.example

    @property
    def init_from_env_params(self) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
        return {"MEMBASE_API_KEY": "mbk_from_env"}, {}, {"name": self.tool_name}


class _Search(_ToolParams):
    tool_class = MembaseSearchTool
    tool_name = "membase_search"
    example = {"query": "what did we decide about the ledger"}


class _Save(_ToolParams):
    tool_class = MembaseSaveTool
    tool_name = "membase_save"
    example = {"content": "I prefer dark mode.", "static": True}


class _Profile(_ToolParams):
    tool_class = MembaseProfileTool
    tool_name = "membase_profile"
    example = {"topic": "working hours"}


class TestSearchToolUnit(_Search, ToolsUnitTests):
    pass


class TestSearchToolIntegration(_Search, ToolsIntegrationTests):
    pass


class TestSaveToolUnit(_Save, ToolsUnitTests):
    pass


class TestSaveToolIntegration(_Save, ToolsIntegrationTests):
    pass


class TestProfileToolUnit(_Profile, ToolsUnitTests):
    pass


class TestProfileToolIntegration(_Profile, ToolsIntegrationTests):
    pass
