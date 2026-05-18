"""
pipeline/nodes/log_pill.py — Node 3: Build the log pill (pure Python, no LLM call)

Transforms all_incidents into a compact pill structure used by:
  - The frontend Error Breakdown table
  - The vector embedding (pill_text)
  - All LLM prompt headers (pill_header)
  - The rendered HTML report

State consumed:  all_incidents, total_raw_lines, app_id, since_seconds
State produced:  log_pill, pill_header, top_errors, window_str, pill_text, sse_events
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


async def log_pill_node(state: dict) -> dict:
    """LangGraph node — build log pill from incidents (no external calls)."""
    all_incidents   = state.get("all_incidents", [])
    total_raw_lines = int(state.get("total_raw_lines", 0))
    app_id          = state.get("app_id", "")
    since_seconds   = int(state.get("since_seconds", 3600))
    events: list[dict] = list(state.get("sse_events", []))

    events.append({
        "step":   "log_pill",
        "status": "running",
        "data":   "Building compact log pill…",
    })

    # ── Human-readable time window ────────────────────────────────────────────
    if since_seconds < 3600:
        window_str = f"last {since_seconds // 60}min"
    else:
        window_str = f"last {since_seconds // 3600}h"

    # ── Top errors list (top 15 incidents → simple dicts for display) ─────────
    top_errors = [
        {
            "type":     (inc.get("error_types") or ["UNKNOWN"])[0],
            "category": inc.get("category", "unknown"),
            "count":    inc.get("count", 0),
            "sample":   (inc.get("cleaned_lines") or [""])[0][:200],
        }
        for inc in all_incidents[:15]
    ]

    # ── Full log pill (display + report rendering) ────────────────────────────
    log_pill = {
        "app_id":                app_id,
        "window":                window_str,
        "total_raw_lines":       total_raw_lines,
        "unique_error_patterns": len(all_incidents),
        "top_errors":            top_errors,
    }

    # ── Compact LLM pill (lean prompt — top 10 errors, truncated samples) ─────
    llm_pill = {
        "app_id":                app_id,
        "window":                window_str,
        "total_raw_lines":       total_raw_lines,
        "unique_error_patterns": len(all_incidents),
        "top_errors": [
            {
                "type":   e["type"],
                "count":  e["count"],
                "sample": e["sample"][:120],
            }
            for e in top_errors[:10]
        ],
    }

    # ── Pill header — single line prepended to every LLM prompt ──────────────
    top_error_summary = ", ".join(
        f"{e['type']} ({e['count']:,})" for e in top_errors[:6]
    )
    pill_header = (
        f"App: '{app_id}' | Window: {window_str} | "
        f"Total errors: {total_raw_lines:,} | Patterns: {len(all_incidents)}\n"
        f"Top error types: {top_error_summary}"
    )

    # ── Pill text for embedding ────────────────────────────────────────────────
    pill_text = " ".join(
        f"{e['type']} {e['sample']}" for e in top_errors
    )[:8000]

    events.append({
        "step":   "log_pill",
        "status": "done",
        "data":   {"incidents": all_incidents[:15], "count": len(all_incidents)},
    })

    return {
        "log_pill":   log_pill,
        "pill_header": pill_header,
        "top_errors":  top_errors,
        "window_str":  window_str,
        "pill_text":   pill_text,
        "sse_events":  events,
    }
