"""
splunk_client.py — Splunk HTTP Event Collector (HEC) and REST API client.

Two distinct APIs are used:

HEC (HTTP Event Collector, port 8088)
  Used for log INGESTION. Events are POSTed to /services/collector/event
  with an Authorization: Splunk <token> header. Only needed if you are
  writing logs INTO Splunk (log generator, demo apps). Not required for
  pure read/query use.

REST API (port 8089, HTTPS)
  Used for log QUERYING. mcp_server uses this to fetch logs for RCA.
  Splunk's REST API is asynchronous: you create a search job, poll until
  it's DONE, then fetch results.

Auth
----
  HEC:  Authorization: Splunk <token>
  REST: HTTP Basic auth (SPLUNK_USERNAME / SPLUNK_PASSWORD)

SSL
---
  SPLUNK_SSL_VERIFY controls SSL certificate verification for the REST API:
    "false"        → disable verification (local Docker Splunk, self-signed cert)
    "true"         → verify using system CA bundle (production Splunk with valid cert)
    "/path/to.pem" → verify using a specific CA bundle file
"""

import json
import logging
import os
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
SPLUNK_HEC_URL    = os.getenv("SPLUNK_HEC_URL",   "http://splunk:8088/services/collector/event")
SPLUNK_REST_URL   = os.getenv("SPLUNK_REST_URL",  "https://splunk:8089")
SPLUNK_HEC_TOKEN  = os.getenv("SPLUNK_HEC_TOKEN", "")
SPLUNK_USERNAME   = os.getenv("SPLUNK_USERNAME",  "admin")   # ← now configurable
SPLUNK_PASSWORD   = os.getenv("SPLUNK_PASSWORD",  "changeme")
SPLUNK_INDEX      = os.getenv("SPLUNK_INDEX",     "rca_logs")

# SSL verification for REST API calls.
# "false" → no verify (dev/self-signed), "true" → system CA, path → custom CA
_ssl_raw = os.getenv("SPLUNK_SSL_VERIFY", "false").strip()
if _ssl_raw.lower() == "false":
    SPLUNK_SSL_VERIFY: bool | str = False
elif _ssl_raw.lower() == "true":
    SPLUNK_SSL_VERIFY = True
else:
    SPLUNK_SSL_VERIFY = _ssl_raw  # treat as path to CA bundle


class SplunkClient:
    """
    Manages Splunk HEC ingestion and REST API queries.

    Instantiate once per process; the underlying httpx clients use
    connection keep-alive automatically.
    """

    def __init__(self):
        self._hec_headers = {
            "Authorization": f"Splunk {SPLUNK_HEC_TOKEN}",
            "Content-Type": "application/json",
        }
        # REST API uses basic auth
        self._rest_auth = (SPLUNK_USERNAME, SPLUNK_PASSWORD)
        self._rest_base = SPLUNK_REST_URL.rstrip("/")

    # ─────────────────────────────────────────────────────────────────────────
    # HEC — Log ingestion (only needed if writing logs into Splunk)
    # ─────────────────────────────────────────────────────────────────────────

    def send_events(self, events: list[dict], batch_size: int = 100) -> dict:
        """
        POST a list of events to Splunk HEC in batches of batch_size.

        Each event dict should have at minimum:
          event — the log message string or dict
          index — target Splunk index (defaults to SPLUNK_INDEX)
        """
        sent = 0
        failed = 0
        batches = 0

        for i in range(0, len(events), batch_size):
            batch = events[i:i + batch_size]
            body = "\n".join(
                json.dumps({**e, "index": e.get("index", SPLUNK_INDEX)})
                for e in batch
            )
            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(SPLUNK_HEC_URL, content=body, headers=self._hec_headers)
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
        spl_query: str,
        earliest: str = "-1h",
        latest: str = "now",
        max_count: int = 5000,
        poll_interval: float = 2.0,
        timeout: float = 120.0,
    ) -> list[dict]:
        """
        Run a Splunk SPL search and return results.

        Workflow (Splunk async search):
          1. POST /services/search/jobs  → creates job, returns sid
          2. Poll GET /services/search/jobs/{sid} until dispatchState == DONE
          3. GET /services/search/jobs/{sid}/results?output_mode=json&count=N
          4. Return list of result dicts
        """
        try:
            sid = self._create_job(spl_query, earliest, latest)
            if not sid:
                return []
            if not self._wait_for_job(sid, poll_interval, timeout):
                return []
            return self._fetch_results(sid, max_count)
        except Exception as exc:
            logger.error("Splunk search error: %s", exc)
            return []

    def _create_job(self, spl_query: str, earliest: str, latest: str) -> Optional[str]:
        """POST to /services/search/jobs. Returns sid (search ID) or None."""
        try:
            with httpx.Client(verify=SPLUNK_SSL_VERIFY, auth=self._rest_auth, timeout=30.0) as client:
                resp = client.post(
                    f"{self._rest_base}/services/search/jobs",
                    data={
                        "search": f"search {spl_query}",
                        "earliest_time": earliest,
                        "latest_time": latest,
                        "output_mode": "json",
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
        """Poll job status until dispatchState == DONE or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with httpx.Client(verify=SPLUNK_SSL_VERIFY, auth=self._rest_auth, timeout=15.0) as client:
                    resp = client.get(
                        f"{self._rest_base}/services/search/jobs/{sid}",
                        params={"output_mode": "json"},
                    )
                if resp.status_code == 200:
                    state = resp.json().get("entry", [{}])[0].get(
                        "content", {}
                    ).get("dispatchState", "")
                    if state == "DONE":
                        return True
                    if state in ("FAILED", "FATAL"):
                        logger.error("Splunk job %s entered state: %s", sid, state)
                        return False
            except Exception as exc:
                logger.warning("Poll job %s exception: %s", sid, exc)
            time.sleep(poll_interval)

        logger.error("Splunk job %s timed out after %.0fs", sid, timeout)
        return False

    def _fetch_results(self, sid: str, max_count: int) -> list[dict]:
        """GET results for a completed search job."""
        try:
            with httpx.Client(verify=SPLUNK_SSL_VERIFY, auth=self._rest_auth, timeout=60.0) as client:
                resp = client.get(
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
        app_id: str,
        start_time: str = "-1h",
        end_time: str = "now",
        max_patterns: int = 1000,
    ) -> dict:
        """
        Fetch deduplicated error patterns from Splunk using SPL stats + token stripping.

        Works for ANY dataset size — Splunk aggregates at source so only
        ≤max_patterns rows are returned over the wire regardless of index size.
        """
        spl_parts = [
            f'index={SPLUNK_INDEX} source_app_id="{app_id}"'
            f' (error OR exception OR fail OR critical OR warn OR fatal)',
            '| eventstats count as _total_events',
            '| spath input=_raw output=_ec path=error_code',
            '| spath input=_raw output=_msg path=message',
            '| eval _key=lower(coalesce(_ec,"") + "|" + coalesce(substr(_msg,1,300),substr(_raw,1,300)))',
            '| rex field=_key mode=sed "s/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/<uuid>/g"',
            '| rex field=_key mode=sed "s/0x[0-9a-f]{4,}/<addr>/g"',
            '| rex field=_key mode=sed "s/[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/<ip>/g"',
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
                sample = r.get("sample", r.get("_raw", ""))
                if not total_raw:
                    try:
                        total_raw = int(r.get("total_raw", 0))
                    except (ValueError, TypeError):
                        pass
                entries.append({
                    "timestamp": "",
                    "raw_line":  sample,
                    "source":    "splunk",
                    "pre_count": pre_count,
                })

            return {
                "entries":         entries,
                "total_raw_lines": total_raw,
                "count":           len(entries),
                "status":          "ok",
            }
        except Exception as exc:
            logger.error("fetch_deduped_patterns error: %s", exc)
            return {"entries": [], "total_raw_lines": 0, "count": 0, "status": f"error: {exc}"}

    def fetch_logs_for_app(
        self,
        app_id: str,
        start_time: str = "-1h",
        end_time: str = "now",
        max_events: int = 5000,
    ) -> list[dict]:
        """Fetch log entries for a specific app from Splunk."""
        spl = (
            f'index={SPLUNK_INDEX} source_app_id="{app_id}" '
            f'| eval raw_line=_raw '
            f'| fields _time, raw_line, source_app_id, host '
            f'| sort _time'
        )
        raw_results = self.search(spl, earliest=start_time, latest=end_time, max_count=max_events)

        entries = []
        for r in raw_results:
            entries.append({
                "timestamp": r.get("_time", ""),
                "raw_line":  r.get("raw_line", r.get("_raw", "")),
                "source":    "splunk",
            })
        return entries

    def health(self) -> bool:
        """Return True if Splunk REST API is reachable."""
        try:
            with httpx.Client(verify=SPLUNK_SSL_VERIFY, auth=self._rest_auth, timeout=5.0) as client:
                resp = client.get(
                    f"{self._rest_base}/services/server/info",
                    params={"output_mode": "json"},
                )
            return resp.status_code == 200
        except Exception:
            return False


# Module-level singleton
_splunk = SplunkClient()


def get_client() -> SplunkClient:
    """Return the module-level SplunkClient singleton."""
    return _splunk
