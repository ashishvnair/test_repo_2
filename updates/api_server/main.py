"""
main.py — FastAPI API Server (port 8000)

Changes from base version:
  - /api/apps now reads from apps.json via app_registry (no pgvector needed)
  - /api/vectordb/* returns {"disabled": true} when pgvector is not available
  - /api/splunk/stats uses per-app Splunk config from app_registry
"""

import logging
import os
import time
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import mcp_client
from .schemas import (
    AppUpsertRequest, BatchProcessRequest, ErrorTriggerRequest,
    LogsRequest, RCAAcceptRequest, RCAProcessRequest, RCARejectRequest,
    RCARerunRequest, VectorSearchRequest,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

try:
    import connector as _conn
    FRONTEND_DIR      = _conn.FRONTEND_DIR
    LOG_GENERATOR_URL = _conn.LOG_GENERATOR_URL
    BATCH_THREADS     = _conn.BATCH_THREADS
    LOG_CHUNK_SIZE    = _conn.LOG_CHUNK_SIZE
except ImportError:
    FRONTEND_DIR      = os.getenv("FRONTEND_DIR", "/app/frontend")
    LOG_GENERATOR_URL = os.getenv("LOG_GENERATOR_URL", "http://log-generator:8090")
    BATCH_THREADS     = int(os.getenv("BATCH_THREADS", "8"))
    LOG_CHUNK_SIZE    = int(os.getenv("LOG_CHUNK_SIZE", "500"))

app = FastAPI(title="RCA Platform API", version="2.0.0")

_stats       = {"reports_added": 0, "reports_rejected": 0}
_gen_threads: dict = {}
_batch_jobs:  dict = {}

_APP_PORTS = {
    "app-alpha": 8101, "app-beta": 8102, "app-gamma": 8103,
    "app-delta": 8104, "app-epsilon": 8105,
}

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_registry():
    """Import app_registry from project root."""
    import importlib, sys
    # project root is two levels up from api_server/
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("app_registry")


def _pgvector_disabled():
    return JSONResponse(
        {"disabled": True, "reason": "pgvector not configured — set PGVECTOR_DSN to enable"},
        status_code=200,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Health & Settings
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def api_health():
    mcp_ok = await mcp_client.health()
    return {
        "status":           "ok" if mcp_ok else "degraded",
        "mcp_status":       "ok" if mcp_ok else "error",
        "reports_added":    _stats["reports_added"],
        "reports_rejected": _stats["reports_rejected"],
    }


@app.get("/api/settings")
async def api_settings():
    return {
        "splunk_rest_url":       os.getenv("SPLUNK_REST_URL", ""),
        "splunk_index":          os.getenv("SPLUNK_INDEX", ""),
        "loki_url":              os.getenv("LOKI_URL", ""),
        "llm_base_url":          os.getenv("LLM_BASE_URL", ""),
        "llm_chat_model":        os.getenv("LLM_CHAT_MODEL", ""),
        "embed_model":           os.getenv("EMBED_MODEL", ""),
        "known_issue_threshold": os.getenv("KNOWN_ISSUE_DISTANCE_THRESHOLD", "0.3"),
        "vector_enabled":        False,
    }


@app.get("/api/settings/health")
async def api_settings_health():
    results = {}
    try:
        async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
            try:
                r = await client.post(os.getenv("SPLUNK_HEC_URL", "http://splunk:8088/services/collector/event"))
                results["splunk_hec"] = "ok" if r.status_code in (200, 400, 401) else "error"
            except Exception:
                results["splunk_hec"] = "unreachable"
            try:
                r = await client.get(os.getenv("LOKI_URL", "http://loki:3100") + "/ready")
                results["loki"] = "ok" if r.status_code == 200 else "error"
            except Exception:
                results["loki"] = "unreachable"
            results["llm"] = "ok" if await mcp_client.health() else "error"
    except Exception as exc:
        results["error"] = str(exc)
    results["pgvector"] = "disabled"
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Splunk stats — per-app if app_id provided
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/splunk/stats")
async def splunk_stats(app_id: str = ""):
    # Try to get per-app Splunk config
    splunk_rest  = os.getenv("SPLUNK_REST_URL",  "https://splunk:8089")
    splunk_user  = os.getenv("SPLUNK_USERNAME",  "admin")
    splunk_pass  = os.getenv("SPLUNK_PASSWORD",  "changeme")
    splunk_index = os.getenv("SPLUNK_INDEX",     "rca_logs")
    ssl_verify   = False

    if app_id:
        try:
            reg = _get_registry()
            cfg = reg.get_app(app_id)
            if cfg:
                splunk_rest  = cfg.get("splunk_rest_url",  splunk_rest)
                splunk_user  = cfg.get("splunk_username",  splunk_user)
                splunk_pass  = cfg.get("splunk_password",  splunk_pass)
                splunk_index = cfg.get("splunk_index",     splunk_index)
                sv = cfg.get("splunk_ssl_verify", "false").strip().lower()
                ssl_verify = False if sv == "false" else (True if sv == "true" else sv)
        except Exception as exc:
            logger.warning("splunk_stats: could not load app config for %s: %s", app_id, exc)

    url = f"{splunk_rest.rstrip('/')}/services/data/indexes/{splunk_index}?output_mode=json"
    try:
        async with httpx.AsyncClient(verify=ssl_verify, timeout=10.0) as client:
            r = await client.get(url, auth=(splunk_user, splunk_pass))
            r.raise_for_status()
            entry   = r.json()["entry"][0]["content"]
            size_mb = float(entry.get("currentDBSizeMB", 0))
            return {
                "event_count": int(entry.get("totalEventCount", 0)),
                "size_mb":     round(size_mb, 1),
                "size_gb":     round(size_mb / 1024, 3),
                "earliest":    entry.get("minTime"),
                "latest":      entry.get("maxTime"),
                "ready":       True,
            }
    except Exception as exc:
        logger.warning("splunk_stats failed: %s", exc)
        return {"event_count": 0, "size_mb": 0, "size_gb": 0,
                "earliest": None, "latest": None, "ready": False}


# ─────────────────────────────────────────────────────────────────────────────
# App Registry — reads from apps.json, no pgvector needed
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/apps")
async def list_apps():
    try:
        reg = _get_registry()
        return {"apps": reg.list_apps()}
    except Exception as exc:
        logger.error("list_apps error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/apps/{app_id}")
async def get_app(app_id: str):
    try:
        reg = _get_registry()
        app_data = reg.get_app_safe(app_id)
        if not app_data:
            return JSONResponse({"error": "not found"}, status_code=404)
        return app_data
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/apps")
async def upsert_app(body: AppUpsertRequest):
    try:
        reg = _get_registry()
        return reg.upsert_app(body.model_dump())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.delete("/api/apps/{app_id}")
async def delete_app(app_id: str):
    try:
        reg = _get_registry()
        ok = reg.delete_app(app_id)
        return {"deleted": ok}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/apps/reload")
async def reload_apps():
    """Force reload apps.json from disk without restarting the server."""
    try:
        reg = _get_registry()
        count = reg.reload()
        return {"ok": True, "apps_loaded": count}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/docker/containers")
async def docker_containers():
    try:
        import subprocess
        result = subprocess.run(
            ["docker", "ps", "--filter", "label=rca.platform.collect=true",
             "--format", "{{.Names}}\t{{.Ports}}\t{{.Labels}}"],
            capture_output=True, text=True, timeout=5,
        )
        containers = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            name  = parts[0].lstrip("/")
            containers.append({"name": name, "container_name": name})
        return {"containers": containers}
    except Exception as exc:
        return {"containers": [], "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Error generation (proxy to demo apps)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/errors/trigger")
async def trigger_error(body: ErrorTriggerRequest):
    port = _APP_PORTS.get(body.app_id, 8101)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"http://{body.app_id}:{port}/trigger-error",
                params={"type": body.error_type, "count": body.count},
            )
        return resp.json()
    except Exception as exc:
        return {"error": str(exc)}


@app.post("/api/errors/start-generation")
async def start_generation(body: dict):
    import threading
    app_id = body.get("app_id", "app-alpha")
    if app_id in _gen_threads and _gen_threads[app_id].is_alive():
        return {"status": "already_running", "app_id": app_id}

    stop_event = threading.Event()

    def _gen_loop():
        port = _APP_PORTS.get(app_id, 8101)
        import time, random, requests as req
        error_types = ["DB_AUTH", "FILE_IO", "HTTP_5XX", "DB_CONN", "TIMEOUT"]
        while not stop_event.is_set():
            try:
                req.post(
                    f"http://{app_id}:{port}/trigger-error",
                    params={"type": random.choice(error_types)},
                    timeout=3,
                )
            except Exception:
                pass
            time.sleep(random.uniform(2, 8))

    t = threading.Thread(target=_gen_loop, daemon=True)
    t.stop_event = stop_event
    t.start()
    _gen_threads[app_id] = t
    return {"status": "started", "app_id": app_id}


@app.post("/api/errors/stop-generation")
async def stop_generation(body: dict):
    app_id = body.get("app_id", "app-alpha")
    t = _gen_threads.get(app_id)
    if t and hasattr(t, "stop_event"):
        t.stop_event.set()
        return {"status": "stopped", "app_id": app_id}
    return {"status": "not_running", "app_id": app_id}


@app.get("/api/errors/generation-status")
async def generation_status():
    return {
        app_id: "running" if t.is_alive() else "stopped"
        for app_id, t in _gen_threads.items()
    }


# ─────────────────────────────────────────────────────────────────────────────
# Logs
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def fetch_logs(app_id: str, since_seconds: int = 3600, source: str = "splunk", max_events: int = 1000):
    tool   = "fetch_logs_splunk" if source == "splunk" else "fetch_logs_loki"
    result = await mcp_client.call_tool(tool, {
        "app_id":     app_id,
        "start_time": f"-{since_seconds}s",
        "end_time":   "now",
        "max_events": max_events,
    })
    return result


# ─────────────────────────────────────────────────────────────────────────────
# RCA Pipeline
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/rca/process")
async def rca_process(body: RCAProcessRequest):
    from pipeline.graph import stream_rca

    async def generate() -> AsyncGenerator[str, None]:
        async for chunk in stream_rca(
            body.app_id, body.since_seconds, body.source,
            skip_vector_check=body.skip_vector_check,
        ):
            yield chunk

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/rca/accept")
async def rca_accept(body: RCAAcceptRequest):
    """Store an accepted RCA report — silently skips if vector store is disabled."""
    _STRIP_KEYS = {"log_pill"}
    report_to_store = {k: v for k, v in body.report.items() if k not in _STRIP_KEYS}
    try:
        result = await mcp_client.call_tool("store_rca_report", {
            "report":       report_to_store,
            "embedding":    body.embedding,
            "app_id":       body.app_id,
            "embed_source": body.embed_source,
        })
        _stats["reports_added"] += 1
        return result
    except Exception as exc:
        # pgvector not available — accept silently (report not persisted)
        logger.warning("store_rca_report unavailable (pgvector disabled?): %s", exc)
        _stats["reports_added"] += 1
        return {"id": None, "total": 0, "stored": False, "reason": "vector store disabled"}


@app.post("/api/rca/reject")
async def rca_reject(body: RCARejectRequest):
    _stats["reports_rejected"] += 1
    return {"rejected": True, "incident_id": body.incident_id}


@app.post("/api/rca/rerun")
async def rca_rerun(body: RCARerunRequest):
    import json
    cleaned_log = "\n".join(body.cleaned_lines[:20])
    result = await mcp_client.call_tool("call_llm", {
        "prompt": (
            f"App: {body.app_id}\nLogs:\n{cleaned_log[:500]}\n\n"
            "Identify the root cause and recommend 3 immediate fix steps."
        ),
        "system":       "You are a senior SRE. Be concise and technical.",
        "max_tokens":   300,
        "is_reasoning": True,
    })
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Vector DB — graceful disabled responses (pgvector not required)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/vectordb/stats")
async def vectordb_stats():
    return {"disabled": True, "total": 0, "reason": "pgvector not configured"}


@app.get("/api/vectordb/search")
async def vectordb_search(query: str, app_id: str = "default", n_results: int = 5):
    return {"disabled": True, "hits": [], "reason": "pgvector not configured"}


@app.get("/api/vectordb/categories")
async def vectordb_categories(app_id: str = "default"):
    return {"disabled": True, "categories": {}, "reason": "pgvector not configured"}


@app.get("/api/vectordb/category/{category}")
async def vectordb_category(category: str, app_id: str = "default", limit: int = 20):
    return {"disabled": True, "reports": [], "reason": "pgvector not configured"}


@app.post("/api/vectordb/reset")
async def vectordb_reset(body: dict):
    return {"disabled": True, "reason": "pgvector not configured"}


# ─────────────────────────────────────────────────────────────────────────────
# Batch Processing
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/batch/process")
async def batch_process(body: BatchProcessRequest):
    import asyncio
    from batch_processor.processor import BatchRCAProcessor

    job_id = f"batch-{body.app_id}-{int(time.time())}"

    async def run_job():
        processor = BatchRCAProcessor(
            app_id=body.app_id,
            since_seconds=body.since_seconds,
            threads=body.threads,
            chunk_size=LOG_CHUNK_SIZE,
            source=body.source,
        )
        _batch_jobs[job_id] = {"status": "running", "started_at": time.time()}
        result = await asyncio.to_thread(processor.run)
        _batch_jobs[job_id] = {"status": "done", "result": result, "finished_at": time.time()}

    asyncio.create_task(run_job())
    return {"job_id": job_id, "status": "started"}


@app.get("/api/batch/status")
async def batch_status(job_id: str = ""):
    if job_id:
        return _batch_jobs.get(job_id, {"status": "not_found"})
    return _batch_jobs


# ─────────────────────────────────────────────────────────────────────────────
# Log Generator
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/log-generator/status")
async def log_generator_status():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{LOG_GENERATOR_URL}/status")
            return resp.json()
    except Exception as exc:
        return {"running": False, "error": str(exc)}


@app.post("/api/log-generator/start")
async def log_generator_start():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{LOG_GENERATOR_URL}/start")
            return resp.json()
    except Exception as exc:
        return {"error": str(exc)}


@app.post("/api/log-generator/stop")
async def log_generator_stop():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{LOG_GENERATOR_URL}/stop")
            return resp.json()
    except Exception as exc:
        return {"error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Serve frontend SPA (must be last)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
