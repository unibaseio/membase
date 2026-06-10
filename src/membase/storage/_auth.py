"""
Authorization header builder for the membase Hub API.

Matches the Go SDK scheme in github.com/unibaseio/da-sdk-go:

    digest = sha256(hash_label || uint64_be(unix_seconds))
    sign   = ECDSA over secp256k1 -> 65 bytes (r || s || v, v in {0,1})
    payload = {
        "Type":"",
        "Addr":"0x...",
        "Time":<int>,
        "Hash":"0x<hex>",
        "Sign":"0x<hex>",
    }
    header_value = compact JSON of payload (no spaces)

The hub side (``sdk.DecodeAuth`` in da-sdk-go) accepts this plain-JSON form
via its fallback branch when the header value is not pure hex.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import time as _time
from typing import Dict, Optional

from eth_keys import keys


def _privkey_bytes(privkey: str) -> bytes:
    if privkey.startswith("0x") or privkey.startswith("0X"):
        privkey = privkey[2:]
    pb = bytes.fromhex(privkey)
    if len(pb) != 32:
        raise ValueError(f"private key must be 32 bytes, got {len(pb)}")
    return pb


def _normalize_addr(addr: str) -> str:
    a = addr.strip()
    if not a:
        raise ValueError("wallet address is empty")
    if not a.startswith("0x"):
        a = "0x" + a
    if len(a) != 42:
        raise ValueError(f"wallet address must be 20 hex bytes (got {len(a)})")
    return a


def _resolve_credentials(
    private_key: Optional[str],
    wallet_address: Optional[str],
) -> tuple[str, str]:
    pk = private_key or os.getenv("MEMBASE_SECRET_KEY", "")
    addr = wallet_address or os.getenv("MEMBASE_ACCOUNT", "")
    if not pk:
        raise RuntimeError(
            "MEMBASE_SECRET_KEY is not set; cannot authenticate against the membase hub"
        )
    if not addr:
        raise RuntimeError(
            "MEMBASE_ACCOUNT is not set; cannot authenticate against the membase hub"
        )
    return pk, _normalize_addr(addr)


def build_authorization(
    *,
    hash_label: bytes = b"hub",
    private_key: Optional[str] = None,
    wallet_address: Optional[str] = None,
    now: Optional[int] = None,
) -> str:
    """Build a single Authorization header *value* the Go hub will accept.

    Args:
        hash_label: arbitrary purpose label (must be non-empty bytes). The Go
            SDK uses labels like b"hub", b"upload", b"register". The hub does
            not verify the label content; only that the recovered address
            matches ``Addr``.
        private_key: hex secret (with or without ``0x``), 32 raw bytes. If
            omitted, read from env ``MEMBASE_SECRET_KEY``.
        wallet_address: 0x-prefixed ETH address. If omitted, read from env
            ``MEMBASE_ACCOUNT``. Must correspond to ``private_key``.
        now: unix seconds override (test only).

    Returns:
        Compact JSON string suitable as an Authorization header value.

    Raises:
        RuntimeError: if credentials are missing.
        ValueError: if credentials are malformed.
    """
    if not hash_label:
        raise ValueError("hash_label must be non-empty")

    pk_hex, addr = _resolve_credentials(private_key, wallet_address)

    ts = int(_time.time()) if now is None else int(now)

    h = hashlib.sha256()
    h.update(hash_label)
    h.update(struct.pack(">Q", ts))
    digest = h.digest()

    pk = keys.PrivateKey(_privkey_bytes(pk_hex))
    sig = pk.sign_msg_hash(digest)
    sig_bytes = sig.to_bytes()  # 65 bytes r||s||v, v in {0,1}
    if len(sig_bytes) != 65:
        raise RuntimeError(f"unexpected signature length {len(sig_bytes)}")

    payload = {
        "Type": "",
        "Addr": addr,
        "Time": ts,
        "Hash": "0x" + hash_label.hex(),
        "Sign": "0x" + sig_bytes.hex(),
    }
    return json.dumps(payload, separators=(",", ":"))


def auth_headers(
    hash_label: bytes = b"hub",
    *,
    extra: Optional[Dict[str, str]] = None,
    private_key: Optional[str] = None,
    wallet_address: Optional[str] = None,
) -> Dict[str, str]:
    """Convenience: return a headers dict containing Authorization (+ extras)."""
    h: Dict[str, str] = {
        "Authorization": build_authorization(
            hash_label=hash_label,
            private_key=private_key,
            wallet_address=wallet_address,
        )
    }
    if extra:
        h.update(extra)
    return h


def signer_address(wallet_address: Optional[str] = None) -> str:
    """Return the lowercase 0x-prefixed wallet address this client signs with."""
    _, addr = _resolve_credentials(None, wallet_address)
    return addr.lower()
