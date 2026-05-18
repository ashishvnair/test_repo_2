"""
app_registry.py — Local JSON-based app registry.

Replaces the pgvector `apps` table with a plain JSON file.
Both api_server and mcp_server import this module.

File location: apps.json in the same directory as this file (project root).

To add or edit an app: edit apps.json directly, then call reload()
or restart the servers. No database needed.

Per-app fields:
  app_id          — unique identifier used throughout the platform
  app_name        — human-readable display name
  splunk_rest_url — REST API URL  e.g. https://splunk.company.com:8089
  splunk_username — Splunk login username
  splunk_password — Splunk login password
  splunk_index    — Splunk index to search
  splunk_ssl_verify — "false" | "true" | "/path/to/cert.pem"
  splunk_hec_url  — HEC URL (optional, only needed for log ingestion)
  splunk_hec_token — HEC token (optional)
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# apps.json lives in the same directory as this file (project root)
_REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apps.json")

# In-memory cache — loaded on first access, refreshed by reload()
_apps_cache: Optional[dict[str, dict]] = None


def _load() -> dict[str, dict]:
    """Load apps.json from disk. Returns {app_id: app_dict}."""
    global _apps_cache
    if not os.path.exists(_REGISTRY_PATH):
        logger.warning("apps.json not found at %s — no apps registered", _REGISTRY_PATH)
        _apps_cache = {}
        return _apps_cache

    try:
        with open(_REGISTRY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        apps = {a["app_id"]: a for a in data.get("apps", []) if "app_id" in a}
        logger.info("app_registry: loaded %d apps from %s", len(apps), _REGISTRY_PATH)
        _apps_cache = apps
        return _apps_cache
    except Exception as exc:
        logger.error("app_registry: failed to load apps.json: %s", exc)
        _apps_cache = _apps_cache or {}
        return _apps_cache


def _get_cache() -> dict[str, dict]:
    """Return cache, loading from disk on first call."""
    if _apps_cache is None:
        return _load()
    return _apps_cache


def reload() -> int:
    """Force reload from disk. Returns number of apps loaded."""
    apps = _load()
    return len(apps)


def list_apps() -> list[dict]:
    """Return all apps as a list (password fields stripped for API responses)."""
    return [_safe_app(a) for a in _get_cache().values()]


def get_app(app_id: str) -> Optional[dict]:
    """Return one app by app_id (with credentials for internal use), or None."""
    return _get_cache().get(app_id)


def get_app_safe(app_id: str) -> Optional[dict]:
    """Return one app with password stripped (safe for API responses)."""
    app = get_app(app_id)
    return _safe_app(app) if app else None


def _safe_app(app: dict) -> dict:
    """Strip sensitive fields before returning to the frontend."""
    return {k: v for k, v in app.items() if k not in ("splunk_password", "splunk_hec_token")}


def upsert_app(app: dict) -> dict:
    """
    Add or update an app in apps.json.
    Writes back to disk immediately.
    """
    if "app_id" not in app:
        raise ValueError("app_id is required")

    cache = _get_cache()
    cache[app["app_id"]] = app

    _write_back(cache)
    logger.info("app_registry: upserted app_id=%s", app["app_id"])
    return _safe_app(app)


def delete_app(app_id: str) -> bool:
    """Remove an app from apps.json. Returns True if it existed."""
    cache = _get_cache()
    if app_id not in cache:
        return False
    del cache[app_id]
    _write_back(cache)
    logger.info("app_registry: deleted app_id=%s", app_id)
    return True


def _write_back(cache: dict[str, dict]) -> None:
    """Persist current cache to apps.json."""
    try:
        data = {"apps": list(cache.values())}
        with open(_REGISTRY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        logger.error("app_registry: failed to write apps.json: %s", exc)
        raise
