"""
server.py — MCP Tool Server (FastAPI, port 8001)

This is the internal tool server. The api-server calls these tools.
Each tool is exposed as POST /tools/{tool_name} accepting JSON arguments.

The architecture stays: UI -> api-server:8000 -> mcp-server:8001/tools/* -> Splunk/pgvector/LLM

Using plain FastAPI endpoints instead of the MCP wire protocol gives us:
  - No MCP library version dependency for the HTTP transport layer
  - Simpler debugging (standard HTTP, curl-able)
  - Same logical separation: all RCA logic lives here, api-server is thin

A /health endpoint is used by docker-compose healthcheck.
A /tools endpoint lists all available tools (mirrors MCP list_tools).
A /update-token endpoint hot-swaps the LLM Bearer token without restart.

Tools (10 total):
  fetch_logs_splunk, fetch_logs_loki, clean_logs, split_incidents,
  generate_embedding, search_similar_rca, store_rca_report,
  call_llm, scratchpad_write, scratchpad_read
"""

import logging
import os
import time
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import scratchpad
from . import pgvector_client as pgdb
from . import splunk_client as splunk_mod
from . import llm_client as llm
from .log_pipeline import (
    clean_log_line,
    split_into_incidents,
    ERROR_TYPE_TO_CATEGORY,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
KNOWN_ISSUE_THRESHOLD = float(os.getenv("KNOWN_ISSUE_DISTANCE_THRESHOLD", "0.3"))

# Minimum composite score (0-1) to surface a stored report as "similar".
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.60"))

# 4-stage matching weights
W_CATEGORY = 0.15
W_ERROR    = 0.25
W_PILL     = 0.25
W_DISTANCE = 0.35

app = FastAPI(title="RCA MCP Tool Server", version="2.0.0")

# ─────────────────────────────────────────────────────────────────────────────
# Startup: ensure DB schema
# ─────────────────────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_startup():
    try:
        pgdb.ensure_schema()
        logger.info("pgvector schema ready")
    except Exception as exc:
        logger.error("Schema init failed (will retry on next DB call): %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Health endpoint
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    db_ok     = pgdb.health()
    llm_ok    = llm.health_check()
    splunk_ok = splunk_mod.get_client().health()
    status    = "ok" if db_ok else "degraded"
    return {
        "status":   status,
        "pgvector": "ok" if db_ok     else "error",
        "llm":      "ok" if llm_ok    else "error",
        "splunk":   "ok" if splunk_ok else "error",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Token refresh endpoint  POST /update-token
# Curl: curl -s -X POST http://localhost:8001/update-token \
#            -H "Content-Type: application/json" \
#            -d "{\"token\": \"YOUR_NEW_TOKEN\"}"
# ─────────────────────────────────────────────────────────────────────────────
class TokenUpdateRequest(BaseModel):
    token: str

@app.post("/update-token")
def update_token(body: TokenUpdateRequest):
    """
    Hot-swap the LLM Bearer token without restarting the MCP server.
    Call this every time you get a new token (every ~20 min).
    """
    if not body.token or not body.token.strip():
        return JSONResponse({"error": "token is empty"}, status_code=400)
    llm.set_auth_token(body.token.strip())
    return {"ok": True, "message": "LLM auth token updated — next call will use the new token"}


# ─────────────────────────────────────────────────────────────────────────────
# Tool registry
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/tools")
def list_tools():
    return {"tools": [
        "fetch_logs_splunk", "fetch_logs_loki", "clean_logs", "split_incidents",
        "generate_embedding", "search_similar_rca", "store_rca_report",
        "call_llm", "scratchpad_write", "scratchpad_read",
    ]}


# ─────────────────────────────────────────────────────────────────────────────
# Generic tool dispatcher  POST /tools/{tool_name}
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/tools/{tool_name}")
async def call_tool(tool_name: str, body: dict):
    try:
        result = _dispatch(tool_name, body)
        return result
    except KeyError as exc:
        return JSONResponse({"error": f"Unknown tool: {tool_name}"}, status_code=404)
    except Exception as exc:
        logger.exception("Tool %s raised exception", tool_name)
        return JSONResponse({"error": str(exc)}, status_code=500)


def _dispatch(tool_name: str, args: dict) -> Any:
    tools = {
        "fetch_logs_splunk":  _tool_fetch_logs_splunk,
        "fetch_logs_loki":    _tool_fetch_logs_loki,
        "clean_logs":         _tool_clean_logs,
        "split_incidents":    _tool_split_incidents,
        "generate_embedding": _tool_generate_embedding,
        "search_similar_rca": _tool_search_similar_rca,
        "store_rca_report":   _tool_store_rca_report,
        "call_llm":           _tool_call_llm,
        "scratchpad_write":   _tool_scratchpad_write,
        "scratchpad_read":    _tool_scratchpad_read,
    }
    if tool_name not in tools:
        raise KeyError(tool_name)
    return tools[tool_name](args)


# ─────────────────────────────────────────────────────────────────────────────
# Tool implementations
# ─────────────────────────────────────────────────────────────────────────────

def _tool_fetch_logs_splunk(args: dict) -> dict:
    try:
        client = splunk_mod.get_client()
        max_events = int(args.get("max_events", 0))

        if max_events == 0:
            result = client.fetch_deduped_patterns(
                app_id=args["app_id"],
                start_time=args.get("start_time", "-1h"),
                end_time=args.get("end_time", "now"),
                max_patterns=1000,
            )
            return result
        else:
            entries = client.fetch_logs_for_app(
                app_id=args["app_id"],
                start_time=args.get("start_time", "-1h"),
                end_time=args.get("end_time", "now"),
                max_events=max_events,
            )
            return {
                "entries":         entries,
                "count":           len(entries),
                "total_raw_lines": len(entries),
                "status":          "ok",
            }
    except Exception as exc:
        logger.error("fetch_logs_splunk error: %s", exc)
        return {"entries": [], "count": 0, "total_raw_lines": 0, "status": f"error: {exc}"}


def _tool_fetch_logs_loki(args: dict) -> dict:
    import httpx
    try:
        now      = time.time()
        end_ns   = args.get("end_time")  or str(int(now * 1e9))
        start_ns = args.get("start_time") or str(int((now - 3600) * 1e9))
        app_id   = args["app_id"]

        resp = httpx.get(f"{LOKI_URL}/loki/api/v1/query_range", params={
            "query":     f'{{service="{app_id}"}} |~ "(?i)error|exception|fail"',
            "start":     start_ns,
            "end":       end_ns,
            "limit":     int(args.get("max_events", 5000)),
            "direction": "forward",
        }, timeout=30.0)

        if resp.status_code != 200:
            return {"entries": [], "count": 0, "status": f"error: Loki {resp.status_code}"}

        results = resp.json().get("data", {}).get("result", [])
        entries = []
        for stream in results:
            for ts_ns, line in stream.get("values", []):
                entries.append({"timestamp": ts_ns, "raw_line": line, "source": "loki"})
        entries.sort(key=lambda e: e["timestamp"])
        return {"entries": entries, "count": len(entries), "status": "ok"}
    except Exception as exc:
        logger.error("fetch_logs_loki error: %s", exc)
        return {"entries": [], "count": 0, "status": f"error: {exc}"}


def _tool_clean_logs(args: dict) -> dict:
    raw_logs = args.get("raw_logs", [])
    cleaned  = [clean_log_line(e.get("raw_line", "")) for e in raw_logs]
    return {"cleaned_lines": cleaned, "count": len(cleaned)}


def _tool_split_incidents(args: dict) -> dict:
    pills = split_into_incidents(
        entries=args.get("entries", []),
        app_id=args.get("app_id", ""),
    )
    return {"incidents": [p.to_dict() for p in pills], "count": len(pills)}


def _tool_generate_embedding(args: dict) -> dict:
    try:
        embedding = llm.embed(args["text"], model=args.get("model", ""))
        return {"embedding": embedding, "dims": len(embedding)}
    except Exception as exc:
        logger.error("generate_embedding error: %s", exc)
        return {"embedding": [], "dims": 0, "error": str(exc)}


def _tool_search_similar_rca(args: dict) -> dict:
    embedding  = args.get("embedding", [])
    app_id     = args.get("app_id", "default")
    n_results  = int(args.get("n_results", 10))

    try:
        raw_hits = pgdb.query_similar(
            embedding=embedding,
            app_id=app_id,
            n_results=n_results,
            distance_threshold=2.0,
        )
    except Exception as exc:
        logger.error("pgvector query error for app_id=%s: %s", app_id, exc, exc_info=True)
        return {"hits": [], "best_distance": 1.0, "best_score": 0.0,
                "is_known_issue": False, "has_similar_reports": False, "similar_reports": []}

    logger.info("search_similar_rca: app_id=%s raw DB hits=%d", app_id, len(raw_hits))
    if not raw_hits:
        return {"hits": [], "best_distance": 1.0, "best_score": 0.0,
                "is_known_issue": False, "has_similar_reports": False, "similar_reports": []}

    incident_text  = args.get("incident_text", "")
    inc_category   = args.get("incident_category", "")
    inc_error_type = args.get("incident_error_type", "")
    inc_pill_text  = args.get("incident_pill_text", "")
    incident_words = set((inc_pill_text or incident_text).lower().split())

    scored = []
    for hit in raw_hits:
        cat_score  = 1.0 if (inc_category   and hit.category   == inc_category)   else 0.0
        err_score  = 1.0 if (inc_error_type and hit.error_type == inc_error_type) else 0.0
        hit_words  = set(str(hit.report).lower().split())
        overlap    = len(incident_words & hit_words)
        pill_score = overlap / max(len(incident_words | hit_words), 1)
        dist_score = max(0.0, 1.0 - hit.cosine_distance)
        final      = (W_CATEGORY * cat_score + W_ERROR * err_score +
                      W_PILL * pill_score + W_DISTANCE * dist_score)

        rep = hit.report if isinstance(hit.report, dict) else {}
        raw_summary = rep.get("summary") or rep.get("root_cause") or rep.get("incident_summary") or ""
        summary_1liner = (raw_summary.split(".")[0].strip() + ".") if raw_summary else "No summary available."

        scored.append({
            "id":             hit.id,
            "app_id":         hit.app_id,
            "distance":       round(hit.cosine_distance, 4),
            "score":          round(final, 4),
            "similarity_pct": round(final * 100, 1),
            "report":         rep,
            "category":       hit.category,
            "error_type":     hit.error_type,
            "created_at":     hit.created_at,
            "summary":        summary_1liner,
            "severity":       rep.get("severity", "medium"),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    best          = scored[0] if scored else None
    best_distance = best["distance"] if best else 1.0
    best_score    = best["score"]    if best else 0.0

    similar_reports = [h for h in scored[:5] if h["score"] >= SIMILARITY_THRESHOLD]
    has_similar     = len(similar_reports) > 0
    is_known        = bool(best and best_score >= 0.55 and best_distance <= KNOWN_ISSUE_THRESHOLD)

    logger.info(
        "search_similar_rca: app_id=%s scored=%d best_score=%.3f "
        "threshold=%.2f has_similar=%s top_scores=%s",
        app_id, len(scored), best_score, SIMILARITY_THRESHOLD, has_similar,
        [round(h["score"], 3) for h in scored[:5]],
    )

    return {
        "hits":                scored,
        "best_distance":       best_distance,
        "best_score":          best_score,
        "is_known_issue":      is_known,
        "has_similar_reports": has_similar,
        "similar_reports":     similar_reports,
    }


def _tool_store_rca_report(args: dict) -> dict:
    embedding = args.get("embedding", [])
    if not embedding:
        msg = "store_rca_report: embedding is empty — report not stored"
        logger.error(msg)
        raise ValueError(msg)

    try:
        report_id = pgdb.insert_report(
            report=args["report"],
            embedding=embedding,
            app_id=args.get("app_id", "default"),
            embed_source=args.get("embed_source", ""),
        )
        total = pgdb.count_all(app_id=args.get("app_id", "default"))
        return {"id": report_id, "total": total}
    except Exception as exc:
        logger.error("store_rca_report error: %s", exc)
        raise


def _tool_call_llm(args: dict) -> dict:
    if args.get("is_reasoning"):
        content = llm.call_llm_reasoning(
            args["prompt"], args["system"],
            max_tokens=int(args.get("max_tokens", 800)),
        )
        return {"content": content, "parsed": None, "attempts": 1, "_failed": False}

    return llm.call_llm(
        prompt=args["prompt"],
        system=args["system"],
        max_tokens=int(args.get("max_tokens", 1500)),
        temperature=float(args.get("temperature", 0.3)),
        max_retries=int(args.get("max_retries", 5)),
        required_keys=args.get("required_keys"),
    )


def _tool_scratchpad_write(args: dict) -> dict:
    scratchpad.write(args["key"], args["value"], ttl_seconds=int(args.get("ttl_seconds", 3600)))
    return {"ok": True, "key": args["key"], "expires_in": args.get("ttl_seconds", 3600)}


def _tool_scratchpad_read(args: dict) -> dict:
    return scratchpad.read(args["key"], default=args.get("default"))
