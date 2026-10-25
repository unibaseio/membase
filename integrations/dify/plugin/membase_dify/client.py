"""The Membase SDK client built from the provider credentials, and errors a person can act on.

Every network call the plugin makes goes through ``open_client``: the official
``unibaseio-membase`` SDK, pointed at the Membase API (or at another Membase host the
workspace administrator set). The base URL is limited to HTTPS on Membase's own domains so
the credential form cannot turn the plugin into a request to an arbitrary host.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from membase import Membase
from membase.client import DEFAULT_TIMEOUT
from membase.errors import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    MembaseError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

# The plugin's own default: it does not depend on which SDK release is installed.
DEFAULT_BASE_URL = "https://api.app.membase.com"
ALLOWED_HOST_SUFFIXES = ("membase.com", "membase.ai", "membase.io")
# Dify allows one tool invocation 300 s (see main.py). Two SDK attempts of 90 s fit;
# the SDK's default of three might not.
MAX_RETRIES = 1
KEYS_PAGE = "Connect › Developer keys in the Membase app (https://app.membase.com)"


class MembaseToolError(Exception):
    """A failure the tool reports to the agent or workflow, with what to do about it."""


def resolve_base_url(raw: Any) -> str:
    """The API root to call: the default, or an HTTPS URL on a Membase domain."""
    value = str(raw or "").strip()
    if not value:
        return DEFAULT_BASE_URL
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as err:
        raise MembaseToolError(f"Base URL is not a valid URL: {value}") from err
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        raise MembaseToolError("Base URL must start with https://")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise MembaseToolError("Base URL must not carry a user, password, query or fragment")
    if port not in (None, 443):
        raise MembaseToolError("Base URL must use the default HTTPS port")
    if parts.path.strip("/"):
        raise MembaseToolError(
            "Base URL is the API root only, for example https://api.app.membase.com"
        )
    if not any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES):
        raise MembaseToolError(
            "Base URL must be a Membase host (membase.com, membase.ai or membase.io); "
            "leave it empty to use https://api.app.membase.com"
        )
    return f"https://{host}"


def api_key_from(credentials: Mapping[str, Any]) -> str:
    key = str(credentials.get("api_key") or "").strip()
    if not key:
        raise MembaseToolError(f"No Membase API key is configured. Create one under {KEYS_PAGE}.")
    return key


def _http_client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=DEFAULT_TIMEOUT)


@contextlib.contextmanager
def open_client(credentials: Mapping[str, Any]) -> Iterator[Membase]:
    """A Membase client for the workspace's credentials, closed when the block ends."""
    api_key = api_key_from(credentials)
    base_url = resolve_base_url(credentials.get("base_url"))
    http = _http_client(base_url)
    try:
        client = Membase(
            api_key=api_key, base_url=base_url, max_retries=MAX_RETRIES, http_client=http
        )
        yield client
    finally:
        http.close()


def explain(err: MembaseError) -> str:
    """One sentence that says what failed and what the person can do. Never includes the key."""
    detail = err.message
    if isinstance(err, PermissionDeniedError):
        return (
            f"Membase refused this request (403: {detail}). The API key is wrong, or its owner "
            "revoked or narrowed it (a Read key cannot save; a container may be outside its "
            f"reach). Do not retry with this key; check it under {KEYS_PAGE}."
        )
    if isinstance(err, AuthenticationError):
        return f"Membase received no API key (401). Add one under {KEYS_PAGE}."
    if isinstance(err, BadRequestError):
        return f"Membase rejected the request (400: {detail})."
    if isinstance(err, NotFoundError):
        return f"Membase could not find that (404: {detail})."
    if isinstance(err, UnprocessableEntityError):
        return (
            f"The Membase account cannot run a memory turn right now (422 {err.code}: {detail}). "
            "Usually the account has no working model under AI Setup, or its free turns are spent."
        )
    if isinstance(err, RateLimitError):
        return f"Membase is busy for this account (429: {detail}). Try again in a minute."
    if isinstance(err, APITimeoutError):
        return (
            "Membase did not answer in time. The first call after a quiet spell can take up to "
            "a minute while the memory wakes; try again."
        )
    if isinstance(err, APIConnectionError):
        return f"Could not reach Membase: {detail}"
    status = f"{err.status}: " if err.status else ""
    return f"Membase error ({status}{detail})."


def container_names(containers: list[dict]) -> str:
    names = [f"{c.get('name') or '(unnamed)'} ({c.get('id')})" for c in containers]
    return ", ".join(names) if names else "none"


def resolve_container(client: Membase, wanted: Any) -> dict | None:
    """The container a tool call named, by id or by name; None when it named none."""
    value = str(wanted or "").strip()
    if not value:
        return None
    containers = client.containers.list().get("containers") or []
    for c in containers:
        if c.get("id") == value:
            return c
    lowered = value.lower()
    matches = [c for c in containers if str(c.get("name") or "").strip().lower() == lowered]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise MembaseToolError(
            f"More than one Membase container is named '{value}': {container_names(matches)}. "
            "Pass the id instead."
        )
    raise MembaseToolError(
        f"No Membase container named '{value}' is in this key's reach. "
        f"Containers available: {container_names(containers)}."
    )
