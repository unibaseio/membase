"""Errors, one class per HTTP status class, all carrying the platform's error envelope."""

from __future__ import annotations

from typing import Any


class MembaseError(Exception):
    """Base class of everything this package raises."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body
        err = body.get("error") if isinstance(body, dict) else None
        err = err if isinstance(err, dict) else {}
        self.code: str = str(err.get("code") or "")
        self.details: Any = err.get("details")
        self.retryable: bool = bool(err.get("retryable", False))
        self.trace_id: str = str(err.get("trace_id") or "")

    def __str__(self) -> str:  # pragma: no cover - formatting
        bits = [self.message]
        if self.status is not None:
            bits.append(f"[{self.status}{' ' + self.code if self.code else ''}]")
        if self.trace_id:
            bits.append(f"trace_id={self.trace_id}")
        return " ".join(bits)


class APIConnectionError(MembaseError):
    """The request never got an answer (DNS, connection refused, reset)."""


class APITimeoutError(APIConnectionError):
    """The request ran past the client's timeout."""


class APIStatusError(MembaseError):
    """A non-2xx answer."""


class BadRequestError(APIStatusError):
    """400 — a malformed request: an empty ``q``, both ``content`` and ``url``, a list ``limit`` outside 1-1000."""


class AuthenticationError(APIStatusError):
    """401 — the request carried no bearer at all."""


class PermissionDeniedError(APIStatusError):
    """403 (``code: unauthorized``) — an unknown, expired or revoked key, or a verb above its access level."""


class NotFoundError(APIStatusError):
    """404 — an id that is not this key's to read: unknown, deleted, outside its reach, or of the wrong type."""


class ConflictError(APIStatusError):
    """409."""


class UnprocessableEntityError(APIStatusError):
    """422 — a request the API cannot take (``validation``: an unknown field such as ``container``,
    a value out of range), or the account's memory cannot run a turn on this deployment
    (``capability_unavailable``)."""


class WriteTargetRequiredError(UnprocessableEntityError):
    """422 ``write_target_required`` — a write from a key that reaches several Memories and has no
    write target. Nothing was written; the key's owner sets where its writes go (no request names
    a Memory)."""


class ConfirmationRequiredError(APIStatusError):
    """428 ``confirmation_required`` — a removal without ``confirm=True``, or one this key may not
    carry out on its own. Nothing changed; ``details`` names the target (``id``, ``verb``, …)."""


class RateLimitError(APIStatusError):
    """429 — the account's turn budget is spent for now."""


class InternalServerError(APIStatusError):
    """5xx."""


_BY_STATUS: dict[int, type[APIStatusError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableEntityError,
    428: ConfirmationRequiredError,
    429: RateLimitError,
}

_BY_CODE: dict[str, type[APIStatusError]] = {
    "write_target_required": WriteTargetRequiredError,
}


def error_for(status: int, body: Any, fallback: str = "") -> APIStatusError:
    """The exception for a non-2xx answer."""
    err = body.get("error") if isinstance(body, dict) else None
    message = (err or {}).get("message") if isinstance(err, dict) else None
    if not message and isinstance(body, dict) and body.get("detail"):
        message = str(body["detail"])
    message = message or fallback or f"HTTP {status}"
    code = err.get("code") if isinstance(err, dict) else None
    cls = _BY_CODE.get(str(code or "")) or _BY_STATUS.get(status) or (
        InternalServerError if status >= 500 else APIStatusError
    )
    return cls(message, status=status, body=body)
