"""
pipeline/nodes/clean_logs.py — Node 2: Classify + fingerprint log entries

Calls MCP tool split_incidents to group entries by fingerprint.
Returns IncidentPill objects sorted by count descending.

State consumed:  entries, total_raw_lines, app_id
State produced:  all_incidents, incident_count, sse_events
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def clean_logs_node(state: dict) -> dict:
    """LangGraph node — classify log entries into IncidentPills via MCP split_incidents."""
    from api_server import mcp_client

    entries         = state.get("entries", [])
    total_raw_lines = int(state.get("total_raw_lines", len(entries)))
    app_id          = state.get("app_id", "")
    events: list[dict] = list(state.get("sse_events", []))

    events.append({
        "step":   "clean_logs",
        "status": "running",
        "data":   f"Classifying {len(entries)} patterns — inferring error types…",
    })

    try:
        dedup_result  = await mcp_client.call_tool(
            "split_incidents", {"entries": entries, "app_id": app_id}
        )
        all_incidents = sorted(
            dedup_result.get("incidents", []) if isinstance(dedup_result, dict) else [],
            key=lambda x: x.get("count", 0),
            reverse=True,
        )
        incident_count = len(all_incidents)

        events.append({
            "step":   "clean_logs",
            "status": "done",
            "data": {
                "count":   incident_count,
                "message": f"{incident_count} unique patterns from {total_raw_lines:,} total log lines",
            },
        })

    except Exception as exc:
        logger.exception("clean_logs_node failed")
        events.append({
            "step":   "clean_logs",
            "status": "error",
            "data":   {"message": str(exc)},
        })
        return {
            "all_incidents":  [],
            "incident_count": 0,
            "error":          str(exc),
            "sse_events":     events,
        }

    return {
        "all_incidents":  all_incidents,
        "incident_count": incident_count,
        "sse_events":     events,
    }
