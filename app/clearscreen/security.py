# -*- coding: utf-8 -*-
"""Client API keys and per-client rate limiting.

A key looks like  cls_<43 random characters>  (256 bits from the OS's secure random source).
Only its SHA-256 hash is stored; a request's key is hashed and looked up. Each key belongs to
exactly one client, and that client is the only one whose whitelist the request may use.
"""
import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from clearscreen.config import SecureStore

KEY_PREFIX = "cls_"
CLIENT_ID_RULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}$")


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_hint(key: str) -> str:
    """What may be shown later to tell keys apart: the first and last few characters."""
    return f"{key[:8]}...{key[-4:]}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def client_for_key(store: SecureStore, key: str | None):
    """(client_id, client) for a valid key of an enabled or disabled client, else (None, None)."""
    if not key or not key.startswith(KEY_PREFIX) or len(key) > 200:
        return None, None
    h = hash_key(key)
    for cid, c in store.clients().items():
        if hmac.compare_digest(c.get("key_hash", ""), h):
            return cid, c
    return None, None


# ----------------------------------------------------- client administration ---
def add_client(store: SecureStore, client_id: str, name: str) -> str:
    """Register a client and return its first API key (shown once)."""
    client_id, name = client_id.strip(), name.strip()
    if not CLIENT_ID_RULE.match(client_id):
        raise ValueError("Client ID: 1-100 characters, letters, digits, '.', '_' or '-', starting with a letter or digit.")
    if not name or len(name) > 200:
        raise ValueError("Client name: 1-200 characters.")
    key = new_key()

    def change(d):
        clients = d.setdefault("clients", {})
        if any(c.lower() == client_id.lower() for c in clients):
            raise ValueError(f"Client ID {client_id} already exists.")
        clients[client_id] = dict(name=name, key_hash=hash_key(key), key_hint=key_hint(key), created_utc=_now(),
                                  rotated_utc=_now(), enabled=True)
    store.update(change)
    return key


def reset_key(store: SecureStore, client_id: str) -> str:
    """Replace a client's API key; the old key stops working at once. Returns the new key (shown once)."""
    key = new_key()

    def change(d):
        c = d.get("clients", {}).get(client_id)
        if c is None:
            raise ValueError(f"No client {client_id}.")
        c.update(key_hash=hash_key(key), key_hint=key_hint(key), rotated_utc=_now())
    store.update(change)
    return key


def set_enabled(store: SecureStore, client_id: str, enabled: bool) -> None:
    def change(d):
        c = d.get("clients", {}).get(client_id)
        if c is None:
            raise ValueError(f"No client {client_id}.")
        c["enabled"] = bool(enabled)
    store.update(change)


def rename_client(store: SecureStore, client_id: str, name: str) -> None:
    name = name.strip()
    if not name or len(name) > 200:
        raise ValueError("Client name: 1-200 characters.")

    def change(d):
        c = d.get("clients", {}).get(client_id)
        if c is None:
            raise ValueError(f"No client {client_id}.")
        c["name"] = name
    store.update(change)


def remove_client(store: SecureStore, client_id: str) -> None:
    def change(d):
        if d.get("clients", {}).pop(client_id, None) is None:
            raise ValueError(f"No client {client_id}.")
    store.update(change)


# ------------------------------------------------------------- rate limits ---
class RateLimiter:
    """Token bucket per client: `per_minute` requests a minute, refilled continuously."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, client_id: str, per_minute: int) -> tuple[bool, float]:
        """(allowed, seconds until the next request would be allowed)."""
        now = time.monotonic()
        rate = per_minute / 60.0
        with self._lock:
            tokens, last = self._buckets.get(client_id, (float(per_minute), now))
            tokens = min(float(per_minute), tokens + (now - last) * rate)
            if tokens >= 1:
                self._buckets[client_id] = (tokens - 1, now)
                return True, 0.0
            self._buckets[client_id] = (tokens, now)
            return False, (1 - tokens) / rate
