"""
batch_processor/processor.py — Threaded batch RCA processor for large log volumes.

Purpose
-------
The interactive RCA pipeline (/api/rca/process) processes the most recent logs
for one app in real-time and caps at ~10 incidents per run. The batch processor
handles the full 1 GB/hr log volume: it fetches all logs for a time window,
splits them into chunks, processes chunks in parallel using a thread pool, and
returns aggregated results.

This is triggered via POST /api/batch/process and runs as a background task
managed by api_server/main.py.

Threading model
---------------
Phase 1 (single thread): Fetch all logs from Splunk for the time window.
  This is a single sequential call — Splunk search jobs are inherently serial.

Phase 2 (ThreadPoolExecutor with N threads): Process chunks in parallel.
  Each worker thread:
    1. clean_logs(chunk)           → stripped log lines
    2. split_incidents(chunk)      → IncidentPill objects
    3. For each incident:
       a. generate_embedding()     → 1024-dim vector
       b. search_similar_rca()     → known or new?
       c. If known  → record result, skip LLM
       d. If unknown:
            scratchpad_write("rca_raw_{id}", call_llm(reasoning))
            context = scratchpad_read("rca_raw_{id}")
            call_llm(synthesis with context) → final JSON

Phase 3 (main thread): Collect Future results, aggregate stats.

asyncio + ThreadPoolExecutor
-----------------------------
The BatchRCAProcessor is designed to be called from an async context via
asyncio.to_thread(processor.run). The processor itself is synchronous — it
uses its own ThreadPoolExecutor for parallelism. Each thread makes HTTP calls
to the MCP server synchronously using requests (not httpx async).

Why call MCP over HTTP instead of importing directly
-----------------------------------------------------
Keeping the call path through the MCP server's HTTP endpoint means:
  1. All tool calls are traced and logged in one place (the MCP server)
  2. The scratchpad is shared with the interactive pipeline (same process)
  3. If MCP tools are updated, batch processing benefits automatically
  4. Batch processor has no local imports of pgvector or Splunk clients

The cost is one HTTP round-trip per tool call per incident. At 8 threads × ~5
tool calls per incident, this is ~40 concurrent HTTP calls — well within
the MCP server's uvicorn capacity.
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

logger = logging.getLogger(__name__)

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://mcp-server:8001")
LOG_CHUNK_SIZE = int(os.getenv("LOG_CHUNK_SIZE", "500"))

# ─────────────────────────────────────────────────────────────────────────────
# MCP tool call (synchronous — runs inside ThreadPoolExecutor threads)
# ─────────────────────────────────────────────────────────────────────────────

def _call_mcp_tool(tool_name: str, arguments: dict, timeout: float = 60.0) -> dict:
    """
    Call a single MCP tool via HTTP POST to the MCP server's StreamableHTTP endpoint.

    This is the synchronous version used by batch worker threads. Each thread
    has its own requests.Session for connection keep-alive.

    The MCP StreamableHTTP protocol expects a JSON body with {method, params}.
    The server responds with the tool's return value as JSON.
    """
    # Use requests.post directly — the MCP StreamableHTTP endpoint accepts
    # a simple JSON-RPC style call when using the call_tool method format.
    # We use the MCP server's /mcp endpoint which accepts tool invocations.
    try:
        resp = requests.post(
            f"{MCP_SERVER_URL}/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
                "id": 1,
            },
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Extract content from MCP response format
            result = data.get("result", {})
            content = result.get("content", [])
            if content:
                raw = content[0].get("text", "{}")
                try:
                    return json.loads(raw)
                except Exception:
                    return {"raw": raw}
            return result
        else:
            logger.warning("MCP tool %s returned %d", tool_name, resp.status_code)
            return {"error": f"HTTP {resp.status_code}"}
    except Exception as exc:
        logger.error("MCP tool %s failed: %s", tool_name, exc)
        return {"error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# LLM prompts (same as api_server/main.py — could be shared via config)
# ─────────────────────────────────────────────────────────────────────────────

REASONING_SYSTEM = """You are a senior site reliability engineer analyzing application errors.
Given log lines, identify: the root cause, which component failed, why it failed,
and what the cascading effects were. Be specific and technical. Write 4-6 bullet points."""

SYNTHESIS_SYSTEM = """You are a senior SRE generating structured Root Cause Analysis reports.
Using the provided technical analysis, produce ONLY a valid JSON object matching this exact schema.
No prose, no markdown, no explanation — only the JSON object."""


def _process_chunk(chunk: list[dict], app_id: str, source: str) -> list[dict]:
    """
    Process one chunk of log entries: clean → split → embed → search → LLM.
    Runs inside a ThreadPoolExecutor worker thread.

    Returns a list of result dicts, one per incident found in the chunk.
    """
    results = []

    # Step 1: Clean logs
    clean_result = _call_mcp_tool("clean_logs", {"raw_logs": chunk})
    if "error" in clean_result:
        logger.warning("clean_logs error: %s", clean_result["error"])
        return results

    # Step 2: Split into incidents
    incidents_result = _call_mcp_tool("split_incidents", {"entries": chunk, "app_id": app_id})
    incidents = incidents_result.get("incidents", [])
    if not incidents:
        return results

    for incident in incidents:
        incident_id = incident.get("incident_id", "unknown")
        error_type = (incident.get("error_types") or ["UNKNOWN"])[0]
        cleaned_text = "\n".join(incident.get("cleaned_lines", []))[:8000]
        cleaned_log = "\n".join(incident.get("cleaned_lines", [])[:20])

        try:
            # Step 3: Embed the incident
            emb_result = _call_mcp_tool("generate_embedding", {"text": cleaned_text})
            embedding = emb_result.get("embedding", [])
            if not embedding:
                results.append({
                    "incident_id": incident_id,
                    "status": "skipped",
                    "reason": "embedding failed",
                })
                continue

            # Step 4: Vector DB search
            search_result = _call_mcp_tool("search_similar_rca", {
                "incident_text": cleaned_text,
                "embedding": embedding,
                "app_id": app_id,
                "incident_category": incident.get("category", ""),
                "incident_error_type": error_type,
                "incident_pill_text": incident_id,
            })

            if search_result.get("is_known_issue"):
                best_hit = search_result["hits"][0] if search_result.get("hits") else {}
                results.append({
                    "incident_id": incident_id,
                    "status": "known",
                    "error_type": error_type,
                    "best_distance": search_result.get("best_distance"),
                    "previous_report": best_hit.get("report"),
                })
                continue

            # Step 5: Unknown incident — 2-pass LLM via scratchpad
            ts_now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())

            # Pass 1: reasoning (free text, stored in scratchpad)
            reasoning_result = _call_mcp_tool("call_llm", {
                "prompt": (
                    f"Analyze these error logs for {app_id}:\n\n"
                    f"Error Type: {error_type}\n\nLog lines:\n{cleaned_log}\n\n"
                    "Provide a technical bullet-point analysis of what failed and why."
                ),
                "system": REASONING_SYSTEM,
                "max_tokens": 800,
                "is_reasoning": True,
            })
            reasoning_text = reasoning_result.get("content", "")

            # Store in scratchpad
            _call_mcp_tool("scratchpad_write", {
                "key": f"rca_raw_{incident_id}",
                "value": reasoning_text,
                "ttl_seconds": 1800,
            })

            # Pass 2: synthesis (JSON)
            synthesis_result = _call_mcp_tool("call_llm", {
                "prompt": (
                    f"Based on this analysis:\n{reasoning_text}\n\n"
                    f"Error type: {error_type}\nApp: {app_id}\n"
                    f"Log sample: {cleaned_log[:500]}\n\n"
                    f'Produce JSON with keys: raw_error, root_cause, fix_steps, also_try_steps, '
                    f'metadata (category, error_code, tags), incident_summary, causal_map, '
                    f'resolution_steps (immediate_mitigation, preventative_action), topic, '
                    f'app_id="{app_id}", error_type="{error_type}", '
                    f'timestamp="{ts_now}", source_type="{source}"'
                ),
                "system": SYNTHESIS_SYSTEM,
                "max_tokens": 1500,
                "max_retries": 5,
                "required_keys": ["raw_error", "root_cause", "fix_steps", "metadata"],
                "is_reasoning": False,
            })

            if synthesis_result.get("_failed"):
                results.append({
                    "incident_id": incident_id,
                    "status": "failed",
                    "error_type": error_type,
                    "reason": "LLM failed after 5 attempts",
                })
            else:
                report = synthesis_result.get("parsed", {})
                results.append({
                    "incident_id": incident_id,
                    "status": "new",
                    "error_type": error_type,
                    "report": report,
                    "embedding": embedding,
                    "app_id": app_id,
                })

        except Exception as exc:
            logger.error("Error processing incident %s: %s", incident_id, exc)
            results.append({
                "incident_id": incident_id,
                "status": "error",
                "reason": str(exc),
            })

    return results


# ─────────────────────────────────────────────────────────────────────────────
# BatchRCAProcessor
# ─────────────────────────────────────────────────────────────────────────────

class BatchRCAProcessor:
    """
    Processes a large volume of logs (e.g. 1 GB from 1 hour) using threads.

    Usage:
      processor = BatchRCAProcessor(app_id="app-alpha", since_seconds=3600, threads=8)
      result = processor.run()   # blocking — run via asyncio.to_thread() from async context
    """

    def __init__(
        self,
        app_id: str,
        since_seconds: int = 3600,
        threads: int = 8,
        chunk_size: int = LOG_CHUNK_SIZE,
        source: str = "splunk",
    ):
        self.app_id = app_id
        self.since_seconds = since_seconds
        self.threads = threads
        self.chunk_size = chunk_size
        self.source = source

    def run(self) -> dict:
        """
        Execute the full batch pipeline. Blocking — run via asyncio.to_thread().

        Returns a summary dict:
          total_incidents  — total distinct incidents found across all chunks
          known_issues     — incidents matched to existing RCA reports
          new_reports      — incidents that produced new LLM RCA reports
          failed           — incidents that failed (LLM error, network error, etc.)
          skipped          — incidents skipped (embedding failed, etc.)
          processing_time  — wall-clock seconds
          threads_used     — actual worker thread count
          chunks_processed — number of chunks processed
        """
        t_start = time.time()
        logger.info(
            "Batch RCA: app=%s since=%ds threads=%d chunk_size=%d source=%s",
            self.app_id, self.since_seconds, self.threads, self.chunk_size, self.source,
        )

        # ── Phase 1: Fetch logs ──────────────────────────────────────────────
        tool = "fetch_logs_splunk" if self.source != "loki" else "fetch_logs_loki"
        fetch_result = _call_mcp_tool(tool, {
            "app_id": self.app_id,
            "start_time": f"-{self.since_seconds}s",
            "end_time": "now",
            "max_events": 50000,  # large cap for 1 GB log volumes
        }, timeout=120.0)

        entries = fetch_result.get("entries", [])
        logger.info("Fetched %d log entries for %s", len(entries), self.app_id)

        if not entries:
            return self._empty_result(t_start, reason="No log entries found")

        # ── Phase 2: Chunk and process in parallel ───────────────────────────
        chunks = [
            entries[i:i + self.chunk_size]
            for i in range(0, len(entries), self.chunk_size)
        ]
        logger.info("Split into %d chunks of up to %d entries each", len(chunks), self.chunk_size)

        all_results = []
        with ThreadPoolExecutor(max_workers=self.threads, thread_name_prefix="batch-rca") as pool:
            futures = {
                pool.submit(_process_chunk, chunk, self.app_id, self.source): i
                for i, chunk in enumerate(chunks)
            }
            for future in as_completed(futures):
                chunk_idx = futures[future]
                try:
                    chunk_results = future.result(timeout=300)
                    all_results.extend(chunk_results)
                    logger.info(
                        "Chunk %d/%d done: %d incidents",
                        chunk_idx + 1, len(chunks), len(chunk_results),
                    )
                except Exception as exc:
                    logger.error("Chunk %d failed: %s", chunk_idx, exc)

        # ── Phase 3: Aggregate stats ─────────────────────────────────────────
        known   = [r for r in all_results if r.get("status") == "known"]
        new     = [r for r in all_results if r.get("status") == "new"]
        failed  = [r for r in all_results if r.get("status") == "failed"]
        errors  = [r for r in all_results if r.get("status") == "error"]
        skipped = [r for r in all_results if r.get("status") == "skipped"]

        elapsed = time.time() - t_start
        logger.info(
            "Batch complete in %.1fs: %d known, %d new, %d failed, %d errors, %d skipped",
            elapsed, len(known), len(new), len(failed), len(errors), len(skipped),
        )

        return {
            "total_incidents":  len(all_results),
            "known_issues":     len(known),
            "new_reports":      len(new),
            "failed":           len(failed) + len(errors),
            "skipped":          len(skipped),
            "processing_time_seconds": round(elapsed, 1),
            "threads_used":     self.threads,
            "chunks_processed": len(chunks),
            "total_entries_fetched": len(entries),
            "results":          all_results,  # full detail for UI display
        }

    def _empty_result(self, t_start: float, reason: str = "") -> dict:
        return {
            "total_incidents": 0,
            "known_issues": 0,
            "new_reports": 0,
            "failed": 0,
            "skipped": 0,
            "processing_time_seconds": round(time.time() - t_start, 1),
            "threads_used": self.threads,
            "chunks_processed": 0,
            "total_entries_fetched": 0,
            "reason": reason,
            "results": [],
        }
