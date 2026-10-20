"""What the optional parts need, and a plain answer when it is missing."""

from __future__ import annotations

import importlib.util
import sys
from typing import Callable

from .errors import MembaseError

INSTALL_LOCAL = "pip install 'unibaseio-membase[local]'"
HOSTED_MCP_URL = "https://api.app.membase.com/mcp-http"


class MissingDependencyError(MembaseError):
    """A local or MCP feature was used without what it needs installed."""


def local_unavailable(
    version: tuple[int, ...] = tuple(sys.version_info[:2]),
    find: Callable[[str], object] = importlib.util.find_spec,
) -> str | None:
    """Why local memory cannot run here, or None when it can."""
    if version < (3, 12):
        return (
            f"local memory needs Python 3.12 or newer; this is Python {version[0]}.{version[1]}. "
            "The hosted API (Membase() with MEMBASE_API_KEY) works on Python 3.10+."
        )
    if find("membase_core") is None:
        return f"local memory needs the engine: {INSTALL_LOCAL}"
    return None


def mcp_unavailable(find: Callable[[str], object] = importlib.util.find_spec) -> str | None:
    """Why ``membase mcp`` cannot run here, or None when it can."""
    if find("mcp") is None:
        return (
            f"membase mcp needs the MCP library: {INSTALL_LOCAL}. For hosted memory an MCP "
            f"client can also connect to {HOSTED_MCP_URL} directly, with nothing installed."
        )
    return None


def require_local() -> None:
    reason = local_unavailable()
    if reason:
        raise MissingDependencyError(reason)


def require_mcp() -> None:
    reason = mcp_unavailable()
    if reason:
        raise MissingDependencyError(reason)
