"""What media memory needs, and a plain answer when it is missing."""

from __future__ import annotations

import importlib.util
import sys
from typing import Callable

from ..requirements import INSTALL_LOCAL, local_unavailable

INSTALL_MEDIA = "pip install 'unibaseio-membase[media]'"


def media_unavailable(
    version: tuple[int, ...] = tuple(sys.version_info[:2]),
    find: Callable[[str], object] = importlib.util.find_spec,
) -> str | None:
    """Why media memory cannot run here, or None when it can."""
    reason = local_unavailable(version, find)
    if reason:
        return reason.replace(INSTALL_LOCAL, INSTALL_MEDIA).replace("local memory", "media memory")
    if find("membase_media") is None or find("PIL") is None:
        return f"media memory needs the engine's media extra: {INSTALL_MEDIA}"
    return None
