"""The ids local memory hands out, spelled as the hosted API spells them: one typed prefix per
resource (``mem_`` a fact, ``pf_`` a profile fact, ``doc_`` a document, ``med_`` a photo or video),
opaque after it.

Behind the prefix is the id the store already uses (``<container>:<n>`` for a fact,
``<container>:<doc id>`` for a document, ``<container>:<media id>`` for media, ``profile:<n>`` for a
profile fact), so nothing is kept
to translate one into the other. A string that is not one of ours names nothing: it reads as not
found, the same as an id for something deleted.
"""

from __future__ import annotations

import base64
from typing import Literal

from .backend import LocalError

Kind = Literal["memory", "profile_fact", "document", "media"]

PREFIX: dict[str, str] = {"memory": "mem", "profile_fact": "pf", "document": "doc", "media": "med"}


def encode(kind: Kind, internal: str) -> str:
    body = base64.urlsafe_b64encode(internal.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{PREFIX[kind]}_{body}"


def decode(kind: Kind, public: str) -> tuple[str, str]:
    """``(container, id in it)`` for a public id, or a 404 for anything that is not one."""
    prefix = f"{PREFIX[kind]}_"
    value = public or ""
    missing = LocalError(404, "not_found", f"{kind} not found", {"id": public})
    if not value.startswith(prefix) or len(value) == len(prefix):
        raise missing
    body = value[len(prefix):]
    try:
        internal = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise missing from None
    # Re-encoding must give back what was asked for, so two spellings never name one thing.
    if not internal or encode(kind, internal) != value:
        raise missing
    container, sep, raw = internal.partition(":")
    if not sep or not container or not raw:
        raise missing
    return container, raw
