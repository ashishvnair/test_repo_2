"""
pipeline/state.py — RCAState TypedDict

All data flowing through the LangGraph pipeline lives in this single TypedDict.
total=False means every field is optional — nodes only populate what they produce.

Field lifecycle:
  fetch_logs      → entries, total_raw_lines, fetch_status
  clean_logs      → all_incidents, incident_count
  log_pill        → log_pill, pill_header, top_errors, window_str, pill_text
  vector_check    → embedding, is_known, best_hit, has_similar_reports, similar_reports
  llm_analysis    → answers, unique_qa
  report_assembly → report, html

Pipeline control fields (sse_events, error) are used by graph.py / stream_rca().
"""

from __future__ import annotations

from typing import Any, Optional
from typing_extensions import TypedDict


class RCAState(TypedDict, total=False):
    # ── Request inputs (set once by stream_rca) ─────────────────────────────
    app_id: str
    since_seconds: int
    source: str                         # "splunk" | "loki"

    # ── After fetch_logs ─────────────────────────────────────────────────────
    entries: list[dict]                 # [{timestamp, raw_line, source}]
    total_raw_lines: int                # true count before dedup (from Splunk eventstats)
    fetch_status: str                   # "ok" | "empty" | "error"

    # ── After clean_logs ─────────────────────────────────────────────────────
    all_incidents: list[dict]           # IncidentPill objects sorted by count desc
    incident_count: int

    # ── After log_pill ───────────────────────────────────────────────────────
    log_pill: dict                      # full pill for display (sent to render_report)
    pill_header: str                    # single-line context prefix for all LLM prompts
    top_errors: list[dict]              # [{type, category, count, sample}] top 15
    window_str: str                     # human-readable window e.g. "last 1h"
    pill_text: str                      # concatenated type+sample string for embedding

    # ── After vector_check ───────────────────────────────────────────────────
    embedding: list[float]              # model embedding (1024-dim)
    is_known: bool                      # True → skip LLM, use cached report
    best_hit: dict                      # closest pgvector hit (empty if not known)
    has_similar_reports: bool           # True → ≥1 stored report scored ≥60% similarity
    similar_reports: list[dict]         # top-5 enriched hit cards for the UI panel
    skip_vector_check: bool             # True → bypass vector search, go straight to LLM

    # ── After llm_analysis ───────────────────────────────────────────────────
    answers: dict[str, str]             # Q01..Q15 → answer text
    unique_qa: list[dict]               # [{id, question, answer}] for Q16+Q17

    # ── After report_assembly ────────────────────────────────────────────────
    report: dict                        # structured report dict (all sections)
    html: str                           # rendered Jinja2 HTML

    # ── Pipeline control ─────────────────────────────────────────────────────
    error: Optional[str]                # set if a node raises an unrecoverable error
    sse_events: list[dict]              # events buffered by each node for stream_rca()
                                        # each item: {"step": str, "status": str, "data": Any}
