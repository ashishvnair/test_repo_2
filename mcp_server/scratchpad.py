"""
scratchpad.py — In-memory TTL key-value store for the RCA pipeline.

Purpose
-------
The RCA pipeline uses a 2-pass LLM pattern to reduce JSON parse failures:

  Pass 1 (reasoning):  LLM produces free-text bullet-point analysis.
                       Result is written here under "rca_raw_{incident_id}".

  Pass 2 (synthesis):  scratchpad_read retrieves the reasoning context.
                       LLM is asked to produce clean JSON using that context.

This separation means the JSON-production step has structured input and
rarely needs retries, dropping failure rate from ~30% to ~5%.

The store is module-level (process singleton). The MCP server is single-process,
so all tool calls share one store. Thread safety is enforced by a Lock because
the batch processor calls MCP tools from multiple threads simultaneously.

Each entry expires after ttl_seconds (default 1 hour) to prevent memory leaks
from abandoned sessions. Expired entries are cleaned lazily on next access.
"""

import threading
import time
from typing import Any

_store: dict[str, tuple[Any, float]] = {}  # key → (value, expires_at unix timestamp)
_lock = threading.Lock()


def write(key: str, value: Any, ttl_seconds: int = 3600) -> str:
    """Store value under key, expiring after ttl_seconds. Returns the key."""
    expires_at = time.time() + ttl_seconds
    with _lock:
        _store[key] = (value, expires_at)
    return key


def read(key: str, default: Any = None) -> dict:
    """
    Read value for key. Returns a dict with:
      found   — True if key exists and has not expired
      expired — True if key existed but expired (value is default)
      value   — the stored value, or default
    """
    with _lock:
        entry = _store.get(key)

    if entry is None:
        return {"found": False, "expired": False, "value": default}

    value, expires_at = entry
    if time.time() > expires_at:
        # Lazy deletion — remove expired entry
        with _lock:
            _store.pop(key, None)
        return {"found": False, "expired": True, "value": default}

    return {"found": True, "expired": False, "value": value}


def delete(key: str) -> None:
    """Remove a key explicitly (used after a pipeline run completes)."""
    with _lock:
        _store.pop(key, None)


def purge_expired() -> int:
    """Remove all expired entries. Returns count of purged keys."""
    now = time.time()
    with _lock:
        expired_keys = [k for k, (_, exp) in _store.items() if now > exp]
        for k in expired_keys:
            del _store[k]
    return len(expired_keys)


def stats() -> dict:
    """Return current store size and count of expired (not yet purged) entries."""
    now = time.time()
    with _lock:
        total = len(_store)
        expired = sum(1 for _, (_, exp) in _store.items() if now > exp)
    return {"total_keys": total, "expired_keys": expired, "live_keys": total - expired}
