"""API-key generation, parsing and hashing.

Key format: ``rag_<env>_<prefix>_<secret>``, e.g. ``rag_local_3f9a1c2b_Xy...``.

* ``prefix`` (8 hex chars) is stored in clear text and used as the lookup key, and
  it's safe to show in UIs and logs.
* The full key is never stored. We keep ``HMAC-SHA256(pepper, key)``. Keys are
  high-entropy random strings, so a fast keyed hash is enough. Slow password hashes
  like argon2 protect *low*-entropy human passwords and would only add latency to
  every request.
* ``hmac.compare_digest`` keeps comparisons constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

KEY_SCHEME = "rag"
PREFIX_LEN = 8


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    """A freshly minted key. ``plaintext`` is shown to the user exactly once."""

    plaintext: str
    prefix: str
    hash: str


@dataclass(frozen=True, slots=True)
class ParsedKey:
    env: str
    prefix: str
    plaintext: str


def hash_api_key(plaintext: str, pepper: str) -> str:
    """Keyed hash of the full key; the pepper lives only in server configuration."""
    return hmac.new(pepper.encode(), plaintext.encode(), hashlib.sha256).hexdigest()


def generate_api_key(env: str, pepper: str) -> GeneratedKey:
    """Create a new random API key (192 bits of secret entropy)."""
    prefix = secrets.token_hex(PREFIX_LEN // 2)
    secret = secrets.token_urlsafe(24)
    plaintext = f"{KEY_SCHEME}_{env}_{prefix}_{secret}"
    return GeneratedKey(plaintext=plaintext, prefix=prefix, hash=hash_api_key(plaintext, pepper))


def parse_api_key(plaintext: str) -> ParsedKey | None:
    """Split a presented key into parts; returns None if it isn't one of ours."""
    # maxsplit=3 because the url-safe secret itself may contain underscores.
    parts = plaintext.split("_", 3)
    if len(parts) != 4 or parts[0] != KEY_SCHEME:
        return None
    _, env, prefix, secret = parts
    if len(prefix) != PREFIX_LEN or not secret:
        return None
    return ParsedKey(env=env, prefix=prefix, plaintext=plaintext)


def verify_api_key(plaintext: str, expected_hash: str, pepper: str) -> bool:
    """Constant-time comparison of a presented key against a stored hash."""
    return hmac.compare_digest(hash_api_key(plaintext, pepper), expected_hash)


def constant_time_equals(a: str, b: str) -> bool:
    """Compare two secrets (e.g. the bootstrap admin key) without timing leaks."""
    return hmac.compare_digest(a.encode(), b.encode())
