"""
connector.py -- Master Connector for the RCA Platform

This is the single source of truth for every integration point in the system.
Change a toggle here and restart the relevant server to change runtime behaviour.

# --------------------------------------------------------------------------
# QUICK SWAP EXAMPLES
# --------------------------------------------------------------------------

  Swap Splunk -> Loki:
    LOG_SOURCE    = "loki"
    ENABLE_SPLUNK = False

  Swap LM Studio -> OpenAI cloud:
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

# --------------------------------------------------------------------------

SECTIONS
  1  -- UI
  2  -- API Server
  3  -- MCP Tool Server
  4  -- AI Models (LLM + Embeddings)
  5  -- Log Sources (Splunk / Loki)
  6  -- Vector Store (pgvector / ChromaDB)
  7  -- Pipeline Nodes (enable / disable each step)
  8  -- Report Output
  9  -- Batch Processing
  10 -- LangGraph Graph (build_rca_graph -- reads all flags above)

# --------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# SECTION 1 -- UI
# --------------------------------------------------------------------------
#
#  ENABLE_FRONTEND  True  -> FastAPI mounts static files + serves index.html
#                   False -> API-only mode (no SPA served)
#  FRONTEND_DIR            Path to frontend static files
#
# --------------------------------------------------------------------------

ENABLE_FRONTEND = True
FRONTEND_DIR    = os.getenv("FRONTEND_DIR", "/app/frontend")


# --------------------------------------------------------------------------
# SECTION 2 -- API SERVER
# --------------------------------------------------------------------------
#
#  API_HOST                   Bind address (0.0.0.0 = all interfaces)
#  API_PORT                   FastAPI port (default 8000)
#  ENABLE_BATCH_ENDPOINTS     True  -> /api/batch/* routes registered
#  ENABLE_ERROR_GEN_ENDPOINTS True  -> /api/errors/* routes registered
#  LOG_GENERATOR_URL          URL of the log-generator service
#
# --------------------------------------------------------------------------

API_HOST  = os.getenv("API_HOST",  "0.0.0.0")
API_PORT  = int(os.getenv("API_PORT", "8000"))

ENABLE_BATCH_ENDPOINTS     = True
ENABLE_ERROR_GEN_ENDPOINTS = True

LOG_GENERATOR_URL = os.getenv("LOG_GENERATOR_URL", "http://log-generator:8090")
BATCH_THREADS     = int(os.getenv("BATCH_THREADS", "8"))
LOG_CHUNK_SIZE    = int(os.getenv("LOG_CHUNK_SIZE", "500"))


# --------------------------------------------------------------------------
# SECTION 3 -- MCP TOOL SERVER
# --------------------------------------------------------------------------
#
#  ENABLE_MCP      True  -> api-server calls mcp-server for all tool operations
#  MCP_SERVER_URL          Base URL of the mcp-server
#  MCP_TIMEOUTS            Per-category timeout in seconds:
#    slow  -- LLM calls, Splunk scans, DB stores (slow by nature)
#    fast  -- embedding generation, scratchpad read/write
#    other -- all other tools (search, clean, split)
#
# --------------------------------------------------------------------------

ENABLE_MCP     = True
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://mcp-server:8001")

MCP_TIMEOUTS = {
    "slow":  360.0,
    "fast":   30.0,
    "other":  60.0,
}

MCP_SLOW_TOOLS = {"call_llm", "fetch_logs_splunk", "fetch_logs_loki", "store_rca_report"}
MCP_FAST_TOOLS = {"generate_embedding", "scratchpad_write", "scratchpad_read"}


# --------------------------------------------------------------------------
# SECTION 4 -- AI MODELS
# --------------------------------------------------------------------------
#
#  USE_LANGCHAIN_LLM         True  -> LangChain ChatOpenAI for Q&A calls
#                            False -> MCP call_llm tool (default, more portable)
#  USE_LANGCHAIN_EMBEDDINGS  True  -> LangChain OpenAIEmbeddings
#                            False -> MCP generate_embedding tool (default)
#
#  LLM_BASE_URL    OpenAI-compatible endpoint
#  LLM_API_KEY     Any non-empty string for LM Studio; real key for OpenAI cloud
#  LLM_CHAT_MODEL  Model identifier
#  LLM_TEMPERATURE Sampling temperature (lower = more deterministic)
#  EMBED_MODEL     Embedding model name
#  EMBED_DIMS      Expected output dimensions
#
# --------------------------------------------------------------------------

USE_LANGCHAIN_LLM        = False
USE_LANGCHAIN_EMBEDDINGS = False

LLM_BASE_URL    = os.getenv("LLM_BASE_URL",    "http://host.docker.internal:1234/v1")
LLM_API_KEY     = os.getenv("LLM_API_KEY",     "lm-studio")
LLM_CHAT_MODEL  = os.getenv("LLM_CHAT_MODEL",  "local-model")
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


# --------------------------------------------------------------------------
# SECTION 5 -- LOG SOURCES
# --------------------------------------------------------------------------
#
#  LOG_SOURCE    "splunk"  -> default
#                "loki"    -> Grafana Loki
#  ENABLE_SPLUNK / ENABLE_LOKI -- guard flags checked by fetch_logs node
#  LOG_SOURCE_TOOL_MAP -- maps source name -> MCP tool name
#
# --------------------------------------------------------------------------

LOG_SOURCE    = os.getenv("LOG_SOURCE", "splunk")
ENABLE_SPLUNK = True
ENABLE_LOKI   = True

LOG_SOURCE_TOOL_MAP: dict[str, str] = {
    "splunk": "fetch_logs_splunk",
    "loki":   "fetch_logs_loki",
}


# --------------------------------------------------------------------------
# SECTION 6 -- VECTOR STORE
# --------------------------------------------------------------------------
#
#  ENABLE_VECTOR_CHECK    True  -> check pgvector before running LLM analysis
#                         False -> always run LLM (bypasses similarity search)
#  VECTOR_BACKEND         "pgvector" | "chroma"
#  SIMILARITY_THRESHOLD   Composite score (0.0-1.0) to surface a stored report
#  PGVECTOR_DSN           PostgreSQL connection string
#
# --------------------------------------------------------------------------

ENABLE_VECTOR_CHECK   = False          # disabled: no pgvector available
VECTOR_BACKEND        = "pgvector"
SIMILARITY_THRESHOLD  = float(os.getenv("SIMILARITY_THRESHOLD", "0.60"))
KNOWN_ISSUE_THRESHOLD = SIMILARITY_THRESHOLD
PGVECTOR_DSN          = os.getenv("PGVECTOR_DSN", "postgresql://rca:rca@localhost:5432/rca_db")

VECTOR_SEARCH_TOOL_MAP: dict[str, str] = {
    "pgvector": "search_similar_rca",
    "chroma":   "search_similar_rca_chroma",
}


# --------------------------------------------------------------------------
# SECTION 7 -- PIPELINE NODES
# --------------------------------------------------------------------------
#
#  Each flag below enables/disables its corresponding LangGraph node.
#  Node order: fetch_logs -> clean_logs -> log_pill -> vector_check -> llm_analysis -> report_assembly
#
#  QA_BATCH_SIZE   Number of questions per LLM batch call (3 = 5 calls for Q01-Q15)
#  QA_MAX_TOKENS   max_tokens per Q&A batch call
#
# --------------------------------------------------------------------------

ENABLE_NODE_FETCH_LOGS      = True
ENABLE_NODE_CLEAN_LOGS      = True
ENABLE_NODE_LOG_PILL        = True
ENABLE_NODE_VECTOR_CHECK    = False    # disabled: no pgvector available
ENABLE_NODE_LLM_ANALYSIS    = True
ENABLE_NODE_REPORT_ASSEMBLY = True

QA_BATCH_SIZE = int(os.getenv("QA_BATCH_SIZE", "3"))
QA_MAX_TOKENS = int(os.getenv("QA_MAX_TOKENS", "400"))


# --------------------------------------------------------------------------
# SECTION 8 -- REPORT OUTPUT
# --------------------------------------------------------------------------
#
#  ENABLE_HTML_REPORT  True  -> render Jinja2 HTML
#                      False -> JSON-only mode
#
# --------------------------------------------------------------------------

ENABLE_HTML_REPORT = True


# --------------------------------------------------------------------------
# SECTION 9 -- BATCH PROCESSING
# --------------------------------------------------------------------------

BATCH_MAX_CHUNKS  = int(os.getenv("BATCH_MAX_CHUNKS",  "50"))
BATCH_MAX_THREADS = int(os.getenv("BATCH_MAX_THREADS", "8"))


# --------------------------------------------------------------------------
# SECTION 10 -- LANGGRAPH GRAPH
# --------------------------------------------------------------------------
#
#  build_rca_graph() -- reads all ENABLE_NODE_* flags, assembles StateGraph,
#                       compiles and returns the runnable graph.
#
#  rca_graph         -- singleton compiled at import time by pipeline/graph.py
#
# --------------------------------------------------------------------------

def build_rca_graph():
    """Assemble and compile the LangGraph StateGraph from ENABLE_NODE_* flags."""
    try:
        from langgraph.graph import StateGraph, END
    except ImportError as exc:
        raise ImportError(
            "langgraph is not installed. "
            "Add to api_server/requirements.txt: langgraph>=0.2.0"
        ) from exc

    from pipeline.state import RCAState
    from pipeline.nodes.fetch_logs      import fetch_logs_node
    from pipeline.nodes.clean_logs      import clean_logs_node
    from pipeline.nodes.log_pill        import log_pill_node
    from pipeline.nodes.vector_check    import vector_check_node
    from pipeline.nodes.llm_analysis    import llm_analysis_node
    from pipeline.nodes.report_assembly import report_assembly_node

    graph = StateGraph(RCAState)

    node_registry = [
        ("fetch_logs",      fetch_logs_node),
        ("clean_logs",      clean_logs_node),
        ("log_pill",        log_pill_node),
        ("vector_check",    vector_check_node),
        ("llm_analysis",    llm_analysis_node),
        ("report_assembly", report_assembly_node),
    ]
    enable_map = {
        "fetch_logs":      ENABLE_NODE_FETCH_LOGS,
        "clean_logs":      ENABLE_NODE_CLEAN_LOGS,
        "log_pill":        ENABLE_NODE_LOG_PILL,
        "vector_check":    ENABLE_NODE_VECTOR_CHECK,
        "llm_analysis":    ENABLE_NODE_LLM_ANALYSIS,
        "report_assembly": ENABLE_NODE_REPORT_ASSEMBLY,
    }

    enabled_nodes = [(name, fn) for name, fn in node_registry if enable_map.get(name, True)]

    if not enabled_nodes:
        raise ValueError("connector.py: all pipeline nodes are disabled -- enable at least one")

    for name, fn in enabled_nodes:
        graph.add_node(name, fn)

    graph.set_entry_point(enabled_nodes[0][0])

    enabled_names = [name for name, _ in enabled_nodes]

    for i in range(len(enabled_names) - 1):
        current = enabled_names[i]
        nxt     = enabled_names[i + 1]
        if current == "vector_check" and ENABLE_NODE_VECTOR_CHECK:
            _wire_vector_conditional(graph, enabled_names, i)
            break
        else:
            graph.add_edge(current, nxt)
    else:
        graph.add_edge(enabled_names[-1], END)

    return graph.compile()


def _wire_vector_conditional(graph, enabled_names: list[str], vc_index: int):
    """Conditional edge from vector_check: known -> report_assembly, new -> llm_analysis."""
    from langgraph.graph import END

    _has_llm    = "llm_analysis"    in enabled_names
    _has_report = "report_assembly" in enabled_names

    def _route(state: dict) -> str:
        if state.get("is_known", False):
            return "report_assembly" if _has_report else "__end__"
        return "llm_analysis" if _has_llm else ("report_assembly" if _has_report else "__end__")

    path_map: dict = {"__end__": END}
    if _has_llm:    path_map["llm_analysis"]    = "llm_analysis"
    if _has_report: path_map["report_assembly"] = "report_assembly"

    graph.add_conditional_edges("vector_check", _route, path_map)

    remaining = enabled_names[vc_index + 1:]
    for i in range(len(remaining) - 1):
        graph.add_edge(remaining[i], remaining[i + 1])
    if remaining:
        graph.add_edge(remaining[-1], END)


# -- Singleton graph compiled at import time ----------------------------------
# pipeline/graph.py imports this. If langgraph is not installed, rca_graph is
# None and the direct-call fallback path in pipeline/graph.py is used.

try:
    rca_graph = build_rca_graph()
    logger.info("LangGraph RCA graph compiled (%d nodes)", sum([
        ENABLE_NODE_FETCH_LOGS, ENABLE_NODE_CLEAN_LOGS, ENABLE_NODE_LOG_PILL,
        ENABLE_NODE_VECTOR_CHECK, ENABLE_NODE_LLM_ANALYSIS, ENABLE_NODE_REPORT_ASSEMBLY,
    ]))
except ImportError:
    rca_graph = None
    logger.warning("langgraph not installed -- pipeline will use direct node calls.")
except Exception as exc:
    rca_graph = None
    logger.error("Failed to compile RCA graph: %s", exc)
