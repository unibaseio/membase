"""Membase — long-term memory for AI."""

from ._version import __version__
from .client import DEFAULT_BASE_URL, Membase
from .requirements import MissingDependencyError
from .errors import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConfirmationRequiredError,
    ConflictError,
    InternalServerError,
    MembaseError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
    WriteTargetRequiredError,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "Membase",
    "MembaseError",
    "MissingDependencyError",
    "APIConnectionError",
    "APITimeoutError",
    "APIStatusError",
    "BadRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "UnprocessableEntityError",
    "WriteTargetRequiredError",
    "ConfirmationRequiredError",
    "RateLimitError",
    "InternalServerError",
    "__version__",
]
