"""Membase long-term memory for Microsoft Agent Framework (Python).

* :class:`MembaseContextProvider` recalls from the user's memory before each run.
* :func:`membase_tools` gives the agent search, save and profile tools.
* :func:`bind_user` names the person a session belongs to, for ``client_for``.
"""

from ._common import bind_user, user_of
from ._provider import MembaseContextProvider
from ._tools import membase_tools
from ._version import __version__

__all__ = ["MembaseContextProvider", "__version__", "bind_user", "membase_tools", "user_of"]
