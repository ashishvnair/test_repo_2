"""
pipeline/nodes/vector_check.py — Node 4: Embedding + pgvector similarity search

Reads from connector:
  ENABLE_VECTOR_CHECK, VECTOR_SEARCH_TOOL_MAP, VECTOR_BACKEND

State consumed:  pill_text, top_errors, app_id, skip_vector_check
State produced:  embedding, is_known, best_hit, has_similar_reports,
                 similar_reports, sse_events

Behaviour
---------
  skip_vector_check=True  → skip search entirely; return has_similar_reports=False
  No similar hits         → has_similar_reports=False; continue to llm_analysis
  ≥1 hit scores ≥60%      → has_similar_reports=True; graph.py emits similar_found
                             and stops the pipeline (AI not invoked)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def vector_check_node(state: dict) -> dict:
    """LangGraph node — generate embedding + search pgvector for similar reports."""
    import connector
    from api_server import mcp_client

    pill_text         = state.get("pill_text", "")
    top_errors        = state.get("top_errors", [])
    app_id            = state.get("app_id", "")
    skip_vector_check = state.get("skip_vector_check", False)
    events: list[dict] = list(state.get("sse_events", []))

    events.append({
        "step":   "vector_check",
        "status": "running",
        "data":   "Searching vector DB for similar reports…",
    })

    embedding: list         = []
    is_known: bool          = False
    best_hit: dict          = {}
    has_similar: bool       = False
    similar_reports: list   = []

    # ── Generate embedding (ALWAYS — needed for storage even when search skipped) ──
    try:
        emb_result = await mcp_client.call_tool("generate_embedding", {"text": pill_text})
        embedding  = emb_result.get("embedding", [])
    except Exception as exc:
        logger.warning("generate_embedding failed: %s", exc)

    # ── Short-circuit: user requested fresh AI analysis — skip search only ────
    if skip_vector_check:
        events.append({
            "step":   "vector_check",
            "status": "done",
            "data": {
                "known_count": 0,
                "new_count":   1,
                "results":     [{"status": "new"}],
                "skipped":     True,
            },
        })
        return {
            "embedding":           embedding,   # real embedding, not []
            "is_known":            False,
            "best_hit":            {},
            "has_similar_reports": False,
            "similar_reports":     [],
            "sse_events":          events,
        }

    # ── Search vector store ────────────────────────────────────────────────────
    search_result: dict = {
        "is_known_issue":     False,
        "has_similar_reports": False,
        "similar_reports":    [],
        "hits":               [],
        "best_distance":      1.0,
    }

    if embedding and connector.ENABLE_VECTOR_CHECK:
        search_tool = connector.VECTOR_SEARCH_TOOL_MAP.get(
            connector.VECTOR_BACKEND, "search_similar_rca"
        )
        try:
            search_result = await mcp_client.call_tool(search_tool, {
                "incident_text":       pill_text,
                "embedding":           embedding,
                "app_id":              app_id,
                "incident_category":   top_errors[0]["category"] if top_errors else "",
                "incident_error_type": top_errors[0]["type"]     if top_errors else "",
                "incident_pill_text":  pill_text[:500],
            })
        except Exception as exc:
            logger.warning("search_similar_rca failed: %s", exc)

    # ── Unpack results ─────────────────────────────────────────────────────────
    has_similar     = search_result.get("has_similar_reports", False)
    similar_reports = search_result.get("similar_reports", [])

    # Legacy is_known support (kept for backward compat with report_assembly)
    is_known = search_result.get("is_known_issue", False)
    if is_known and search_result.get("hits"):
        best_hit = search_result["hits"][0]

    events.append({
        "step":   "vector_check",
        "status": "done",
        "data": {
            "known_count":          1 if is_known else 0,
            "new_count":            0 if is_known else 1,
            "has_similar_reports":  has_similar,
            "similar_count":        len(similar_reports),
            "results":              [{"status": "known" if is_known else "new"}],
        },
    })

    return {
        "embedding":           embedding,
        "is_known":            is_known,
        "best_hit":            best_hit,
        "has_similar_reports": has_similar,
        "similar_reports":     similar_reports,
        "sse_events":          events,
    }
