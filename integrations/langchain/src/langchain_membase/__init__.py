"""Membase long-term memory for LangChain and LangGraph."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._common import DEFAULT_RECALL_TIMEOUT, MissingUserIdError
from .retrievers import MembaseRetriever, MembaseUnavailableError
from .tools import (
    MembaseProfileTool,
    MembaseSaveTool,
    MembaseSearchTool,
    MembaseToolkit,
    membase_tools,
)

if TYPE_CHECKING:
    from .middleware import MembaseRecallMiddleware

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_RECALL_TIMEOUT",
    "MembaseProfileTool",
    "MembaseRecallMiddleware",
    "MembaseRetriever",
    "MembaseSaveTool",
    "MembaseSearchTool",
    "MembaseToolkit",
    "MembaseUnavailableError",
    "MissingUserIdError",
    "membase_tools",
    "__version__",
]


def __getattr__(name: str) -> Any:
    # The middleware needs `langchain` (the `agents` extra); importing the package must not.
    if name == "MembaseRecallMiddleware":
        try:
            from .middleware import MembaseRecallMiddleware
        except ImportError as e:
            raise ImportError(
                "MembaseRecallMiddleware needs LangChain v1: "
                "pip install 'langchain-membase[agents]'"
            ) from e

        return MembaseRecallMiddleware
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
