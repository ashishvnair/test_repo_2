"""
pipeline/nodes/fetch_logs.py — Node 1: Fetch logs from Splunk or Loki

Reads from connector:
  ENABLE_SPLUNK, ENABLE_LOKI, LOG_SOURCE, LOG_SOURCE_TOOL_MAP

State consumed:  app_id, since_seconds, source
State produced:  entries, total_raw_lines, fetch_status, sse_events
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def fetch_logs_node(state: dict) -> dict:
    """LangGraph node — fetch raw log entries from Splunk or Loki via MCP."""
    import connector
    from api_server import mcp_client

    app_id        = state.get("app_id", "")
    since_seconds = int(state.get("since_seconds", 3600))
    source        = state.get("source", connector.LOG_SOURCE)

    events: list[dict] = list(state.get("sse_events", []))

    # Resolve tool name from connector map (allows hot-swap of log source)
    tool_map  = connector.LOG_SOURCE_TOOL_MAP
    tool_name = tool_map.get(source, "fetch_logs_splunk")

    # Guard: respect enable flags
    if source == "splunk" and not connector.ENABLE_SPLUNK:
        logger.warning("fetch_logs: Splunk is disabled in connector — falling back to loki")
        tool_name = tool_map.get("loki", "fetch_logs_loki")
    elif source == "loki" and not connector.ENABLE_LOKI:
        logger.warning("fetch_logs: Loki is disabled in connector — falling back to splunk")
        tool_name = tool_map.get("splunk", "fetch_logs_splunk")

    events.append({
        "step":   "fetch_logs",
        "status": "running",
        "data":   f"Scanning ALL logs in Splunk for {app_id} — deduplicating at source…",
    })

    try:
        logs_result = await mcp_client.call_tool(tool_name, {
            "app_id":     app_id,
            "start_time": f"-{since_seconds}s",
            "end_time":   "now",
            "max_events": 0,    # 0 = no cap; Splunk returns pre-aggregated patterns
        })
        entries         = logs_result.get("entries", [])
        total_raw_lines = int(logs_result.get("total_raw_lines", len(entries)))
        fetch_status    = "ok" if entries else "empty"

        events.append({
            "step":   "fetch_logs",
            "status": "done",
            "data": {
                "count":   total_raw_lines,
                "entries": entries[:60],
                "message": f"{total_raw_lines:,} total lines → {len(entries)} unique patterns returned",
            },
        })

    except Exception as exc:
        logger.exception("fetch_logs_node failed")
        events.append({
            "step":   "fetch_logs",
            "status": "error",
            "data":   {"message": str(exc)},
        })
        return {
            "entries":         [],
            "total_raw_lines": 0,
            "fetch_status":    "error",
            "error":           str(exc),
            "sse_events":      events,
        }

    return {
        "entries":         entries,
        "total_raw_lines": total_raw_lines,
        "fetch_status":    fetch_status,
        "sse_events":      events,
    }
