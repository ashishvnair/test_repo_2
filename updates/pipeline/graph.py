"""
pipeline/graph.py -- LangGraph StateGraph wrapper + SSE streaming

stream_rca(app_id, since_seconds, source)
  Async generator -- called by api_server/main.py /api/rca/process route.
  Iterates connector.rca_graph.astream(), drains sse_events from each node,
  and yields SSE-formatted strings.

  If connector.rca_graph is None (langgraph not installed), falls back to
  calling each enabled node directly in sequence.

build_rca_graph()
  Thin re-export of connector.build_rca_graph() for external use.

SSE event format (unchanged from original main.py -- frontend requires no changes):
  data: {"step": "fetch_logs", "status": "running", "data": "..."}
  data: {"step": "fetch_logs", "status": "done",    "data": {...}}
  ...
  data: {"step": "complete",   "status": "done",    "data": {"results": [...]}}

Fix: 'complete' event now carries a 'pipeline_status' field:
  "ok"          -- normal results
  "empty"       -- no logs in time window (Splunk returned 0 results)
  "fetch_error" -- Splunk/Loki connection failed
  "no_patterns" -- logs fetched but no error patterns found
The frontend uses this to stay on the pipeline page (not navigate to results)
when there is nothing to show.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

logger = logging.getLogger(__name__)


def _sse(step: str, status: str, data=None) -> str:
    """Format one SSE event string."""
    return f"data: {json.dumps({'step': step, 'status': status, 'data': data})}\n\n"


async def stream_rca(
    app_id: str,
    since_seconds: int,
    source: str,
    skip_vector_check: bool = False,
) -> AsyncGenerator[str, None]:
    """
    Async generator -- runs the full RCA pipeline and yields SSE event strings.

    Called by: api_server/main.py POST /api/rca/process
    """
    import connector

    initial_state = {
        "app_id":              app_id,
        "since_seconds":       since_seconds,
        "source":              source,
        "sse_events":          [],
        "answers":             {},
        "unique_qa":           [],
        "entries":             [],
        "all_incidents":       [],
        "embedding":           [],
        "is_known":            False,
        "best_hit":            {},
        "has_similar_reports": False,
        "similar_reports":     [],
        "skip_vector_check":   skip_vector_check,
        "error":               None,
    }

    try:
        if connector.rca_graph is not None:
            # -- LangGraph path (preferred) ----------------------------------------
            accumulated: dict = dict(initial_state)

            async for chunk in connector.rca_graph.astream(initial_state):
                node_name, node_output = next(iter(chunk.items()))

                # Merge this node's output into accumulated state
                accumulated.update({k: v for k, v in node_output.items() if k != "sse_events"})

                for event in node_output.get("sse_events", []):
                    yield _sse(event["step"], event["status"], event.get("data"))

                # -- Early exit: fetch_logs returned nothing ----------------------
                if node_name == "fetch_logs":
                    if not accumulated.get("entries"):
                        fetch_status = accumulated.get("fetch_status", "empty")
                        error_msg    = accumulated.get("error", "")

                        if fetch_status == "error":
                            # Connection failed -- error SSE already emitted by node
                            yield _sse("complete", "done", {
                                "results":         [],
                                "pipeline_status": "fetch_error",
                                "message":         error_msg or "Could not connect to log source",
                            })
                        else:
                            # Connected fine but no logs in the time window
                            yield _sse("complete", "done", {
                                "results":         [],
                                "pipeline_status": "empty",
                                "message":         "No logs found in the selected time window",
                            })
                        return

                # -- Early exit: no error patterns after clean ---------------
                if node_name == "clean_logs":
                    if not accumulated.get("all_incidents"):
                        yield _sse("complete", "done", {
                            "results":         [],
                            "pipeline_status": "no_patterns",
                            "message":         "No error patterns found in logs",
                        })
                        return

                # -- Similar reports found: surface panel, skip LLM ----------
                if node_name == "vector_check":
                    if accumulated.get("has_similar_reports", False):
                        yield _sse("complete", "done", {
                            "status":          "similar_found",
                            "similar_reports": accumulated.get("similar_reports", []),
                            "app_id":          app_id,
                            "results":         [],
                        })
                        return

                # -- Final result after report_assembly ----------------------
                if node_name == "report_assembly":
                    report        = accumulated.get("report", {})
                    all_incidents = accumulated.get("all_incidents", [])
                    is_known_flag = accumulated.get("is_known", False)
                    best_hit_val  = accumulated.get("best_hit", {})

                    yield _sse("complete", "done", {
                        "pipeline_status": "ok",
                        "results": [{
                            "status":            "known" if is_known_flag else "new",
                            "incident":          all_incidents[0] if all_incidents else {},
                            "report":            report,
                            "embedding":         accumulated.get("embedding", []),
                            "matched_report_id": best_hit_val.get("id", "") if is_known_flag else "",
                        }],
                        "total": 1,
                    })

        else:
            # -- Direct node fallback (no langgraph installed) -------------------
            async for chunk in _run_nodes_direct(initial_state, connector):
                yield chunk

    except Exception as exc:
        logger.exception("stream_rca pipeline error")
        yield _sse("error", "error", {"message": str(exc)})


async def _run_nodes_direct(
    state: dict,
    connector,
) -> AsyncGenerator[str, None]:
    """
    Fallback: call each enabled node directly in sequence (no LangGraph).
    Yields the same SSE events as the graph path.
    """
    from pipeline.nodes.fetch_logs      import fetch_logs_node
    from pipeline.nodes.clean_logs      import clean_logs_node
    from pipeline.nodes.log_pill        import log_pill_node
    from pipeline.nodes.vector_check    import vector_check_node
    from pipeline.nodes.llm_analysis    import llm_analysis_node
    from pipeline.nodes.report_assembly import report_assembly_node

    node_registry = [
        ("fetch_logs",      fetch_logs_node,       connector.ENABLE_NODE_FETCH_LOGS),
        ("clean_logs",      clean_logs_node,       connector.ENABLE_NODE_CLEAN_LOGS),
        ("log_pill",        log_pill_node,         connector.ENABLE_NODE_LOG_PILL),
        ("vector_check",    vector_check_node,     connector.ENABLE_NODE_VECTOR_CHECK),
        ("llm_analysis",    llm_analysis_node,     connector.ENABLE_NODE_LLM_ANALYSIS),
        ("report_assembly", report_assembly_node,  connector.ENABLE_NODE_REPORT_ASSEMBLY),
    ]

    current_state = dict(state)

    for node_name, node_fn, enabled in node_registry:
        if not enabled:
            continue

        # Known-issue short-circuit: skip llm_analysis if is_known
        if node_name == "llm_analysis" and current_state.get("is_known", False):
            continue

        current_state["sse_events"] = []
        output = await node_fn(current_state)
        current_state.update(output)

        for event in output.get("sse_events", []):
            yield _sse(event["step"], event["status"], event.get("data"))

        # -- Early exit: fetch_logs returned nothing ---------------------------
        if node_name == "fetch_logs" and not current_state.get("entries"):
            fetch_status = current_state.get("fetch_status", "empty")
            error_msg    = current_state.get("error", "")

            if fetch_status == "error":
                yield _sse("complete", "done", {
                    "results":         [],
                    "pipeline_status": "fetch_error",
                    "message":         error_msg or "Could not connect to log source",
                })
            else:
                yield _sse("complete", "done", {
                    "results":         [],
                    "pipeline_status": "empty",
                    "message":         "No logs found in the selected time window",
                })
            return

        # -- Early exit: no error patterns ------------------------------------
        if node_name == "clean_logs" and not current_state.get("all_incidents"):
            yield _sse("complete", "done", {
                "results":         [],
                "pipeline_status": "no_patterns",
                "message":         "No error patterns found in logs",
            })
            return

        # -- Similar reports found: surface panel, skip LLM ------------------
        if node_name == "vector_check" and current_state.get("has_similar_reports", False):
            yield _sse("complete", "done", {
                "status":          "similar_found",
                "similar_reports": current_state.get("similar_reports", []),
                "app_id":          current_state.get("app_id", ""),
                "results":         [],
            })
            return

    # -- Final result ---------------------------------------------------------
    report        = current_state.get("report", {})
    all_inc       = current_state.get("all_incidents", [])
    is_known_flag = current_state.get("is_known", False)
    best_hit_val  = current_state.get("best_hit", {})

    yield _sse("complete", "done", {
        "pipeline_status": "ok",
        "results": [{
            "status":            "known" if is_known_flag else "new",
            "incident":          all_inc[0] if all_inc else {},
            "report":            report,
            "embedding":         current_state.get("embedding", []),
            "matched_report_id": best_hit_val.get("id", "") if is_known_flag else "",
        }],
        "total": 1,
    })


def build_rca_graph():
    """Re-export of connector.build_rca_graph() for external use."""
    import connector
    return connector.build_rca_graph()
