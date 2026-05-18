"""
connector.py — Master Connector for the RCA Platform

This is the single source of truth for every integration point in the system.
Change a toggle here and rebuild the api-server container to change runtime behaviour.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK SWAP EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Swap Splunk → Loki:
    LOG_SOURCE    = "loki"
    ENABLE_SPLUNK = False

  Swap LM Studio → OpenAI cloud:
    LLM_BASE_URL   = "https://api.openai.com/v1"
    LLM_API_KEY    = "sk-..."
    LLM_CHAT_MODEL = "gpt-4o"

  Disable LLM entirely (vector-check only, no new analysis):
    ENABLE_NODE_LLM_ANALYSIS = False

  Disable vector store (always run LLM):
    ENABLE_NODE_VECTOR_CHECK = False

  JSON-only report (no HTML render):
    ENABLE_HTML_REPORT = False

  Use LangChain instead of MCP for LLM calls:
    USE_LANGCHAIN_LLM = True

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SECTIONS
  1  — UI
  2  — API Server
  3  — MCP Tool Server
  4  — AI Models (LLM + Embeddings)
  5  — Log Sources (Splunk / Loki)
  6  — Vector Store (pgvector / ChromaDB)
  7  — Pipeline Nodes (enable / disable each step)
  8  — Report Output
  9  — Batch Processing
  10 — LangGraph Graph (build_rca_graph — reads all flags above)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — UI
# ─────────────────────────────────────────────────────────────────────────────
#
#  ENABLE_FRONTEND  True  → FastAPI mounts static files + serves index.html
#                  False  → API-only mode (no SPA served)
#  FRONTEND_DIR            Path to frontend static files inside the container
#
# Input:  none
# Output: served at http://localhost:8000/
# ─────────────────────────────────────────────────────────────────────────────

ENABLE_FRONTEND = True
FRONTEND_DIR    = os.getenv("FRONTEND_DIR", "/app/frontend")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — API SERVER
# ─────────────────────────────────────────────────────────────────────────────
#
#  API_HOST                  Bind address (0.0.0.0 = all interfaces)
#  API_PORT                  FastAPI port (default 8000)
#  ENABLE_BATCH_ENDPOINTS    True  → /api/batch/* routes registered
#  ENABLE_ERROR_GEN_ENDPOINTS True → /api/errors/* routes registered
#  LOG_GENERATOR_URL         URL of the log-generator service
#
# Input:  HTTP requests from browser
# Output: SSE stream, JSON responses
# ─────────────────────────────────────────────────────────────────────────────

API_HOST  = os.getenv("API_HOST",  "0.0.0.0")
API_PORT  = int(os.getenv("API_PORT",  "8000"))

ENABLE_BATCH_ENDPOINTS     = True
ENABLE_ERROR_GEN_ENDPOINTS = True

LOG_GENERATOR_URL = os.getenv("LOG_GENERATOR_URL", "http://log-generator:8090")
BATCH_THREADS     = int(os.getenv("BATCH_THREADS", "8"))
LOG_CHUNK_SIZE    = int(os.getenv("LOG_CHUNK_SIZE", "500"))


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — MCP TOOL SERVER
# ─────────────────────────────────────────────────────────────────────────────
#
#  ENABLE_MCP      True  → api-server calls mcp-server for all tool operations
#                 False  → would use direct implementations (not currently wired)
#  MCP_SERVER_URL          Base URL of the mcp-server container
#  MCP_TIMEOUTS            Per-category timeout in seconds:
#    slow  — LLM calls, Splunk scans, DB stores (slow by nature)
#    fast  — embedding generation, scratchpad read/write
#    other — all other tools (search, clean, split)
#
# Input:  tool_name (str) + arguments (dict)
# Output: dict result from MCP tool
# ─────────────────────────────────────────────────────────────────────────────

ENABLE_MCP     = True
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://mcp-server:8001")

MCP_TIMEOUTS = {
    "slow":  360.0,   # call_llm, fetch_logs_splunk, fetch_logs_loki, store_rca_report
    "fast":   30.0,   # generate_embedding, scratchpad_write, scratchpad_read
    "other":  60.0,   # split_incidents, search_similar_rca, clean_logs, scratchpad_*
}

MCP_SLOW_TOOLS  = {"call_llm", "fetch_logs_splunk", "fetch_logs_loki", "store_rca_report"}
MCP_FAST_TOOLS  = {"generate_embedding", "scratchpad_write", "scratchpad_read"}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — AI MODELS
# ─────────────────────────────────────────────────────────────────────────────
#
#  USE_LANGCHAIN_LLM         True  → LangChain ChatOpenAI for Q&A calls
#                            False → MCP call_llm tool (default, more portable)
#  USE_LANGCHAIN_EMBEDDINGS  True  → LangChain OpenAIEmbeddings
#                            False → MCP generate_embedding tool (default)
#
#  LLM_BASE_URL    OpenAI-compatible endpoint — works with LM Studio, vLLM, etc.
#  LLM_API_KEY     Any non-empty string for LM Studio; real key for OpenAI cloud
#  LLM_CHAT_MODEL  Model identifier (LM Studio uses filename; OpenAI uses model ID)
#  LLM_TEMPERATURE Sampling temperature (lower = more deterministic)
#
#  EMBED_MODEL     Embedding model name
#  EMBED_DIMS      Expected output dimensions (1024 for nomic-embed-text)
#
# Input:  text prompt (str) or text to embed (str)
# Output: model response text (str) or embedding vector (list[float])
#
# Lazy singletons — calling get_chat_model() / get_embed_model() returns a
# cached instance; reconstructed only if you call _reset_model_cache()
# ─────────────────────────────────────────────────────────────────────────────

USE_LANGCHAIN_LLM        = False   # set True to route LLM calls through LangChain
USE_LANGCHAIN_EMBEDDINGS = False   # set True to route embeddings through LangChain

LLM_BASE_URL   = os.getenv("LLM_BASE_URL",   "http://host.docker.internal:1234/v1")
LLM_API_KEY    = os.getenv("LLM_API_KEY",    "lm-studio")
LLM_CHAT_MODEL = os.getenv("LLM_CHAT_MODEL", "local-model")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))

EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-nomic-embed-text-v1-5")
EMBED_DIMS  = int(os.getenv("EMBED_DIMS", "1024"))

_chat_model_cache: Optional[Any]  = None
_embed_model_cache: Optional[Any] = None


def get_chat_model() -> Any:
    """Return cached ChatOpenAI instance (creates on first call)."""
    global _chat_model_cache
    if _chat_model_cache is None:
        from ai.llm import get_chat_model as _build
        _chat_model_cache = _build(
            base_url=LLM_BASE_URL,
            api_key=LLM_API_KEY,
            model=LLM_CHAT_MODEL,
            temperature=LLM_TEMPERATURE,
        )
        logger.info("LangChain ChatOpenAI initialised: model=%s base_url=%s", LLM_CHAT_MODEL, LLM_BASE_URL)
    return _chat_model_cache


def get_embed_model() -> Any:
    """Return cached OpenAIEmbeddings instance (creates on first call)."""
    global _embed_model_cache
    if _embed_model_cache is None:
        from ai.embeddings import get_embed_model as _build
        _embed_model_cache = _build(
            base_url=LLM_BASE_URL,
            api_key=LLM_API_KEY,
            model=EMBED_MODEL,
        )
        logger.info("LangChain OpenAIEmbeddings initialised: model=%s", EMBED_MODEL)
    return _embed_model_cache


def _reset_model_cache() -> None:
    """Force re-creation of model singletons (useful after config change)."""
    global _chat_model_cache, _embed_model_cache
    _chat_model_cache  = None
    _embed_model_cache = None


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — LOG SOURCES
# ─────────────────────────────────────────────────────────────────────────────
#
#  LOG_SOURCE    "splunk"  → default; Splunk deduplicates via eventstats
#                "loki"    → Grafana Loki
#                "unified" → try both, merge results (future)
#  ENABLE_SPLUNK / ENABLE_LOKI — guard flags checked by fetch_logs node
#
#  LOG_SOURCE_TOOL_MAP — maps source name → MCP tool name
#    Swap Splunk → Loki: set LOG_SOURCE="loki", ENABLE_SPLUNK=False
#    Add new source: add entry to LOG_SOURCE_TOOL_MAP + implement MCP tool
#
# Input:  app_id (str), since_seconds (int), source (str)
# Output: entries list[dict], total_raw_lines int
# ─────────────────────────────────────────────────────────────────────────────

LOG_SOURCE    = os.getenv("LOG_SOURCE", "splunk")   # "splunk" | "loki"
ENABLE_SPLUNK = True
ENABLE_LOKI   = True

LOG_SOURCE_TOOL_MAP: dict[str, str] = {
    "splunk": "fetch_logs_splunk",
    "loki":   "fetch_logs_loki",
}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — VECTOR STORE
# ─────────────────────────────────────────────────────────────────────────────
#
#  ENABLE_VECTOR_CHECK       True  → check pgvector before running LLM analysis
#                            False → always run LLM (bypasses similarity search)
#  VECTOR_BACKEND            "pgvector" | "chroma" (chroma requires adding MCP tool)
#  SIMILARITY_THRESHOLD      Composite score (0.0-1.0) at which a stored report is
#                            considered "similar enough" to surface in the UI panel.
#                            0.60 = 60% similarity required (recommended).
#                            Reports scoring above this threshold are shown to the
#                            user in a "Similar Reports" panel; AI is NOT auto-invoked.
#  PGVECTOR_DSN              PostgreSQL connection string for pgvector
#
#  VECTOR_SEARCH_TOOL_MAP    Maps backend name → MCP tool name
#    Swap pgvector → ChromaDB: VECTOR_BACKEND="chroma" (+ implement chroma MCP tool)
#
# Input:  embedding list[float], incident_text str, app_id str
# Output: has_similar_reports bool, similar_reports list[dict], best_hit dict
# ─────────────────────────────────────────────────────────────────────────────

ENABLE_VECTOR_CHECK      = True
VECTOR_BACKEND           = "pgvector"   # "pgvector" | "chroma"
SIMILARITY_THRESHOLD     = float(os.getenv("SIMILARITY_THRESHOLD", "0.60"))
KNOWN_ISSUE_THRESHOLD    = SIMILARITY_THRESHOLD   # backward-compat alias
PGVECTOR_DSN             = os.getenv(
    "PGVECTOR_DSN",
    "postgresql://rca_user:rca_pass@pgvector:5432/rca_db",
)

VECTOR_SEARCH_TOOL_MAP: dict[str, str] = {
    "pgvector": "search_similar_rca",
    "chroma":   "search_similar_rca_chroma",   # add mcp tool to enable
}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — PIPELINE NODES
# ─────────────────────────────────────────────────────────────────────────────
#
#  Each flag below enables/disables its corresponding LangGraph node.
#  Disabled nodes are skipped and the graph re-wires edges around them.
#
#  Node execution order:
#    fetch_logs → clean_logs → log_pill → vector_check → llm_analysis → report_assembly
#
#  ENABLE_NODE_VECTOR_CHECK = False:
#    Replaces conditional "known/new" edge with direct log_pill → llm_analysis edge
#    (LLM always runs — no cache check)
#
#  ENABLE_NODE_LLM_ANALYSIS = False:
#    Graph ends at vector_check (useful for testing vector store only)
#    WARNING: report_assembly also skipped (no Q&A answers available)
#
#  QA_BATCH_SIZE   Number of questions per LLM batch call (3 = 5 calls for Q01-Q15)
#  QA_MAX_TOKENS   max_tokens per Q&A batch call
#
# Input:  RCAState dict
# Output: RCAState dict (merged by LangGraph)
# ─────────────────────────────────────────────────────────────────────────────

ENABLE_NODE_FETCH_LOGS      = True
ENABLE_NODE_CLEAN_LOGS      = True
ENABLE_NODE_LOG_PILL        = True
ENABLE_NODE_VECTOR_CHECK    = True
ENABLE_NODE_LLM_ANALYSIS    = True
ENABLE_NODE_REPORT_ASSEMBLY = True

QA_BATCH_SIZE = int(os.getenv("QA_BATCH_SIZE", "3"))    # 3 = 5 batches for 15 questions
QA_MAX_TOKENS = int(os.getenv("QA_MAX_TOKENS", "400"))   # tokens per batch response


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — REPORT OUTPUT
# ─────────────────────────────────────────────────────────────────────────────
#
#  ENABLE_HTML_REPORT  True  → render Jinja2 HTML (13 sections, returned as report["html"])
#                      False → JSON-only mode (report dict returned without html field)
#
# Input:  report dict, log_pill dict, reasoning_text str
# Output: HTML string (or empty string if disabled)
# ─────────────────────────────────────────────────────────────────────────────

ENABLE_HTML_REPORT = True


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 9 — BATCH PROCESSING
# ─────────────────────────────────────────────────────────────────────────────
#
#  BATCH_MAX_CHUNKS    Maximum number of log chunks per batch job
#  BATCH_MAX_THREADS   Worker threads for parallel chunk processing
#
# Input:  app_id, since_seconds, source, threads, chunk_size
# Output: batch job result dict
# ─────────────────────────────────────────────────────────────────────────────

BATCH_MAX_CHUNKS  = int(os.getenv("BATCH_MAX_CHUNKS",  "50"))
BATCH_MAX_THREADS = int(os.getenv("BATCH_MAX_THREADS", "8"))


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 10 — LANGGRAPH GRAPH
# ─────────────────────────────────────────────────────────────────────────────
#
#  build_rca_graph()  — reads all flags in sections 5-9, assembles a StateGraph,
#                       compiles it, and returns the runnable graph.
#
#  rca_graph          — singleton compiled graph used by pipeline/graph.py
#
#  How nodes are wired:
#    1. Only nodes with ENABLE_NODE_* = True are added
#    2. set_entry_point() finds the first enabled node
#    3. Linear edges between consecutive enabled nodes
#    4. Conditional edge after vector_check:
#         is_known=True  → skip llm_analysis → report_assembly
#         is_known=False → llm_analysis → report_assembly
#    5. If ENABLE_NODE_VECTOR_CHECK=False: direct log_pill → llm_analysis edge
#
# ─────────────────────────────────────────────────────────────────────────────

def build_rca_graph():
    """
    Assemble and compile the LangGraph StateGraph.

    Reads ENABLE_NODE_* flags to include/exclude nodes.
    Returns a compiled LangGraph graph (supports .astream()).

    Raises ImportError if langgraph is not installed.
    """
    try:
        from langgraph.graph import StateGraph, END
    except ImportError as exc:
        raise ImportError(
            "langgraph is not installed. "
            "Add to api_server/requirements.txt: langgraph>=0.2.0"
        ) from exc

    from pipeline.state import RCAState
    from pipeline.nodes.fetch_logs    import fetch_logs_node
    from pipeline.nodes.clean_logs    import clean_logs_node
    from pipeline.nodes.log_pill      import log_pill_node
    from pipeline.nodes.vector_check  import vector_check_node
    from pipeline.nodes.llm_analysis  import llm_analysis_node
    from pipeline.nodes.report_assembly import report_assembly_node

    graph = StateGraph(RCAState)

    # ── Register enabled nodes ────────────────────────────────────────────────
    node_registry: list[tuple[str, object]] = [
        ("fetch_logs",       fetch_logs_node),
        ("clean_logs",       clean_logs_node),
        ("log_pill",         log_pill_node),
        ("vector_check",     vector_check_node),
        ("llm_analysis",     llm_analysis_node),
        ("report_assembly",  report_assembly_node),
    ]
    enable_map = {
        "fetch_logs":      ENABLE_NODE_FETCH_LOGS,
        "clean_logs":      ENABLE_NODE_CLEAN_LOGS,
        "log_pill":        ENABLE_NODE_LOG_PILL,
        "vector_check":    ENABLE_NODE_VECTOR_CHECK,
        "llm_analysis":    ENABLE_NODE_LLM_ANALYSIS,
        "report_assembly": ENABLE_NODE_REPORT_ASSEMBLY,
    }

    enabled_nodes = [
        (name, fn) for name, fn in node_registry if enable_map.get(name, True)
    ]

    if not enabled_nodes:
        raise ValueError("connector.py: all pipeline nodes are disabled — enable at least one")

    for name, fn in enabled_nodes:
        graph.add_node(name, fn)

    # ── Entry point ───────────────────────────────────────────────────────────
    graph.set_entry_point(enabled_nodes[0][0])

    # ── Wire edges between consecutive enabled nodes ──────────────────────────
    enabled_names = [name for name, _ in enabled_nodes]

    for i in range(len(enabled_names) - 1):
        current = enabled_names[i]
        nxt     = enabled_names[i + 1]

        # Special case: after vector_check — conditional branch
        if current == "vector_check" and ENABLE_NODE_VECTOR_CHECK:
            _wire_vector_conditional(graph, enabled_names, i)
            break   # conditional wiring handles remaining edges
        else:
            graph.add_edge(current, nxt)
    else:
        # No vector_check or it's the last node — wire final edge to END
        graph.add_edge(enabled_names[-1], END)

    return graph.compile()


def _wire_vector_conditional(graph, enabled_names: list[str], vc_index: int):
    """
    Add conditional edge from vector_check:
      is_known=True  → skip llm_analysis → go to report_assembly (or END)
      is_known=False → next node (llm_analysis if enabled, else report_assembly)
    """
    from langgraph.graph import END

    _has_llm    = "llm_analysis"    in enabled_names
    _has_report = "report_assembly" in enabled_names

    def _route(state: dict) -> str:
        if state.get("is_known", False):
            return "report_assembly" if _has_report else "__end__"
        return "llm_analysis" if _has_llm else ("report_assembly" if _has_report else "__end__")

    # Build path map — only include destinations that exist
    path_map: dict = {"__end__": END}
    if _has_llm:
        path_map["llm_analysis"]    = "llm_analysis"
    if _has_report:
        path_map["report_assembly"] = "report_assembly"

    graph.add_conditional_edges("vector_check", _route, path_map)

    # Wire remaining nodes after vector_check in sequence
    remaining = enabled_names[vc_index + 1:]
    for i in range(len(remaining) - 1):
        graph.add_edge(remaining[i], remaining[i + 1])
    if remaining:
        graph.add_edge(remaining[-1], END)


# ── Singleton graph compiled at import time ───────────────────────────────────
# api_server/main.py and pipeline/graph.py both import this.
# If langgraph is not installed, rca_graph is None and the fallback
# direct-call path in pipeline/graph.py is used.

try:
    rca_graph = build_rca_graph()
    logger.info("LangGraph RCA graph compiled (%d nodes)", sum([
        ENABLE_NODE_FETCH_LOGS, ENABLE_NODE_CLEAN_LOGS, ENABLE_NODE_LOG_PILL,
        ENABLE_NODE_VECTOR_CHECK, ENABLE_NODE_LLM_ANALYSIS, ENABLE_NODE_REPORT_ASSEMBLY,
    ]))
except ImportError:
    rca_graph = None
    logger.warning(
        "langgraph not installed — pipeline will use direct node calls. "
        "Add langgraph>=0.2.0 to api_server/requirements.txt to enable graph mode."
    )
except Exception as exc:
    rca_graph = None
    logger.error("Failed to compile RCA graph: %s", exc)
