"""
splunk_client.py — Splunk REST API and HEC client.

Supports per-app Splunk config: each app in apps.json can have its own
splunk_rest_url, splunk_username, splunk_password, splunk_index, splunk_ssl_verify.

Use get_client()            → default singleton (from env vars / .env)
Use get_client_for_app(cfg) → per-app client built from apps.json entry
"""

import json
import logging
import os
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Default configuration (from .env — used when no per-app config found)
# ─────────────────────────────────────────────────────────────────────────────
SPLUNK_HEC_URL   = os.getenv("SPLUNK_HEC_URL",   "http://splunk:8088/services/collector/event")
SPLUNK_REST_URL  = os.getenv("SPLUNK_REST_URL",  "https://splunk:8089")
SPLUNK_HEC_TOKEN = os.getenv("SPLUNK_HEC_TOKEN", "")
SPLUNK_USERNAME  = os.getenv("SPLUNK_USERNAME",  "admin")
SPLUNK_PASSWORD  = os.getenv("SPLUNK_PASSWORD",  "changeme")
SPLUNK_INDEX     = os.getenv("SPLUNK_INDEX",     "rca_logs")

_ssl_raw = os.getenv("SPLUNK_SSL_VERIFY", "false").strip()
if _ssl_raw.lower() == "false":
    SPLUNK_SSL_VERIFY: bool | str = False
elif _ssl_raw.lower() == "true":
    SPLUNK_SSL_VERIFY = True
else:
    SPLUNK_SSL_VERIFY = _ssl_raw


def _parse_ssl(value: str) -> bool | str:
    v = str(value).strip().lower()
    if v == "false": return False
    if v == "true":  return True
    return value  # path to cert


class SplunkClient:
    """
    Manages Splunk HEC ingestion and REST API queries.

    Can be constructed with default env-var config or with explicit
    per-app config from apps.json.
    """

    def __init__(
        self,
        rest_url:   str       = SPLUNK_REST_URL,
        username:   str       = SPLUNK_USERNAME,
        password:   str       = SPLUNK_PASSWORD,
        index:      str       = SPLUNK_INDEX,
        ssl_verify: bool|str  = SPLUNK_SSL_VERIFY,
        hec_url:    str       = SPLUNK_HEC_URL,
        hec_token:  str       = SPLUNK_HEC_TOKEN,
    ):
        self._rest_base  = rest_url.rstrip("/")
        self._rest_auth  = (username, password)
        self._index      = index
        self._ssl_verify = ssl_verify
        self._hec_url    = hec_url
        self._hec_headers = {
            "Authorization": f"Splunk {hec_token}",
            "Content-Type": "application/json",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # HEC — Log ingestion
    # ─────────────────────────────────────────────────────────────────────────

    def send_events(self, events: list[dict], batch_size: int = 100) -> dict:
        sent = 0; failed = 0; batches = 0
        for i in range(0, len(events), batch_size):
            batch = events[i:i + batch_size]
            body  = "\n".join(
                json.dumps({**e, "index": e.get("index", self._index)}) for e in batch
            )
            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(self._hec_url, content=body, headers=self._hec_headers)
                if resp.status_code == 200:
                    sent += len(batch)
                else:
                    logger.warning("HEC batch failed: %d %s", resp.status_code, resp.text[:200])
                    failed += len(batch)
            except Exception as exc:
                logger.error("HEC send exception: %s", exc)
                failed += len(batch)
            batches += 1
        return {"sent": sent, "failed": failed, "batches": batches}

    # ─────────────────────────────────────────────────────────────────────────
    # REST API — Log querying
    # ─────────────────────────────────────────────────────────────────────────

    def search(
        self,
        spl_query:     str,
        earliest:      str   = "-1h",
        latest:        str   = "now",
        max_count:     int   = 5000,
        poll_interval: float = 2.0,
        timeout:       float = 120.0,
    ) -> list[dict]:
        try:
            sid = self._create_job(spl_query, earliest, latest)
            if not sid: return []
            if not self._wait_for_job(sid, poll_interval, timeout): return []
            return self._fetch_results(sid, max_count)
        except Exception as exc:
            logger.error("Splunk search error: %s", exc)
            return []

    def _create_job(self, spl_query: str, earliest: str, latest: str) -> Optional[str]:
        try:
            with httpx.Client(verify=self._ssl_verify, auth=self._rest_auth, timeout=30.0) as c:
                resp = c.post(
                    f"{self._rest_base}/services/search/jobs",
                    data={
                        "search":       f"search {spl_query}",
                        "earliest_time": earliest,
                        "latest_time":   latest,
                        "output_mode":   "json",
                    },
                )
            if resp.status_code not in (200, 201):
                logger.error("Create job failed: %d %s", resp.status_code, resp.text[:300])
                return None
            return resp.json().get("sid")
        except Exception as exc:
            logger.error("Create search job exception: %s", exc)
            return None

    def _wait_for_job(self, sid: str, poll_interval: float, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with httpx.Client(verify=self._ssl_verify, auth=self._rest_auth, timeout=15.0) as c:
                    resp = c.get(
                        f"{self._rest_base}/services/search/jobs/{sid}",
                        params={"output_mode": "json"},
                    )
                if resp.status_code == 200:
                    state = resp.json().get("entry", [{}])[0].get("content", {}).get("dispatchState", "")
                    if state == "DONE":   return True
                    if state in ("FAILED", "FATAL"):
                        logger.error("Splunk job %s state: %s", sid, state)
                        return False
            except Exception as exc:
                logger.warning("Poll job %s exception: %s", sid, exc)
            time.sleep(poll_interval)
        logger.error("Splunk job %s timed out after %.0fs", sid, timeout)
        return False

    def _fetch_results(self, sid: str, max_count: int) -> list[dict]:
        try:
            with httpx.Client(verify=self._ssl_verify, auth=self._rest_auth, timeout=60.0) as c:
                resp = c.get(
                    f"{self._rest_base}/services/search/jobs/{sid}/results",
                    params={"output_mode": "json", "count": max_count},
                )
            if resp.status_code != 200:
                logger.error("Fetch results failed: %d", resp.status_code)
                return []
            return resp.json().get("results", [])
        except Exception as exc:
            logger.error("Fetch results exception: %s", exc)
            return []

    def fetch_deduped_patterns(
        self,
        app_id:       str,
        start_time:   str = "-1h",
        end_time:     str = "now",
        max_patterns: int = 1000,
    ) -> dict:
        spl_parts = [
            f'index={self._index} source_app_id="{app_id}"'
            f' (error OR exception OR fail OR critical OR warn OR fatal)',
            '| eventstats count as _total_events',
            '| spath input=_raw output=_ec path=error_code',
            '| spath input=_raw output=_msg path=message',
            '| eval _key=lower(coalesce(_ec,"") + "|" + coalesce(substr(_msg,1,300),substr(_raw,1,300)))',
            '| rex field=_key mode=sed "s/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/<uuid>/g"',
            '| rex field=_key mode=sed "s/0x[0-9a-f]{4,}/<addr>/g"',
            '| rex field=_key mode=sed "s/[0-9]{1,3}\\.[0-9]{1,3}\\.[0-9]{1,3}\\.[0-9]{1,3}/<ip>/g"',
            '| rex field=_key mode=sed "s/[0-9]{5,}/<n>/g"',
            '| stats count, first(_raw) as sample, first(_total_events) as total_raw by _key',
            '| sort -count',
            f'| head {max_patterns}',
        ]
        spl = ' '.join(spl_parts)
        try:
            sid = self._create_job(spl, start_time, end_time)
            if not sid:
                return {"entries": [], "total_raw_lines": 0, "count": 0, "status": "error: could not create job"}
            if not self._wait_for_job(sid, poll_interval=3.0, timeout=600.0):
                return {"entries": [], "total_raw_lines": 0, "count": 0, "status": "error: job timed out"}

            raw_results = self._fetch_results(sid, max_count=max_patterns)
            total_raw = 0
            entries: list[dict] = []
            for r in raw_results:
                pre_count = int(r.get("count", 1))
                sample    = r.get("sample", r.get("_raw", ""))
                if not total_raw:
                    try:    total_raw = int(r.get("total_raw", 0))
                    except: pass
                entries.append({"timestamp": "", "raw_line": sample, "source": "splunk", "pre_count": pre_count})
            return {"entries": entries, "total_raw_lines": total_raw, "count": len(entries), "status": "ok"}
        except Exception as exc:
            logger.error("fetch_deduped_patterns error: %s", exc)
            return {"entries": [], "total_raw_lines": 0, "count": 0, "status": f"error: {exc}"}

    def fetch_logs_for_app(
        self,
        app_id:     str,
        start_time: str = "-1h",
        end_time:   str = "now",
        max_events: int = 5000,
    ) -> list[dict]:
        spl = (
            f'index={self._index} source_app_id="{app_id}" '
            f'| eval raw_line=_raw '
            f'| fields _time, raw_line, source_app_id, host '
            f'| sort _time'
        )
        raw_results = self.search(spl, earliest=start_time, latest=end_time, max_count=max_events)
        return [{"timestamp": r.get("_time", ""), "raw_line": r.get("raw_line", r.get("_raw", "")), "source": "splunk"}
                for r in raw_results]

    def health(self) -> bool:
        try:
            with httpx.Client(verify=self._ssl_verify, auth=self._rest_auth, timeout=5.0) as c:
                resp = c.get(f"{self._rest_base}/services/server/info", params={"output_mode": "json"})
            return resp.status_code == 200
        except Exception:
            return False


# ─────────────────────────────────────────────────────────────────────────────
# Singletons
# ─────────────────────────────────────────────────────────────────────────────

_default_client: Optional[SplunkClient] = None
_app_clients: dict[str, SplunkClient]   = {}   # cache keyed by app_id


def get_client() -> SplunkClient:
    """Return the default singleton (uses .env / global config)."""
    global _default_client
    if _default_client is None:
        _default_client = SplunkClient()
    return _default_client


def get_client_for_app(app_cfg: dict) -> SplunkClient:
    """
    Return a SplunkClient configured for a specific app from apps.json.
    Clients are cached by app_id so we don't recreate on every call.
    Falls back to default client if no Splunk config in app_cfg.
    """
    app_id = app_cfg.get("app_id", "")
    if app_id and app_id in _app_clients:
        return _app_clients[app_id]

    rest_url = app_cfg.get("splunk_rest_url", "").strip()
    if not rest_url:
        return get_client()  # no per-app config, use default

    ssl_raw = app_cfg.get("splunk_ssl_verify", "false")
    client  = SplunkClient(
        rest_url   = rest_url,
        username   = app_cfg.get("splunk_username", SPLUNK_USERNAME),
        password   = app_cfg.get("splunk_password", SPLUNK_PASSWORD),
        index      = app_cfg.get("splunk_index",    SPLUNK_INDEX),
        ssl_verify = _parse_ssl(ssl_raw),
        hec_url    = app_cfg.get("splunk_hec_url",   SPLUNK_HEC_URL),
        hec_token  = app_cfg.get("splunk_hec_token", SPLUNK_HEC_TOKEN),
    )
    if app_id:
        _app_clients[app_id] = client
        logger.info("Built Splunk client for app_id=%s → %s / index=%s",
                    app_id, rest_url, app_cfg.get("splunk_index", SPLUNK_INDEX))
    return client
