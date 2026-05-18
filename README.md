# RCA Platform v2

Automated Root Cause Analysis for containerised applications. Ingests logs from Splunk or Loki, runs AI-powered 15-question diagnostic analysis via a local LLM (LM Studio), stores results in pgvector, and streams the full pipeline as Server-Sent Events to a React-style browser dashboard.

---

## Table of Contents

1. [Platform Overview](#1-platform-overview)
   - [Similar Reports Panel](#similar-reports-panel-new-ux-flow)
2. [Quick Start](#2-quick-start)
3. [Architecture Deep Dive](#3-architecture-deep-dive)
4. [connector.py Reference](#4-connectorpy-reference)
5. [MCP Tool Reference](#5-mcp-tool-reference)
6. [AI / LLM Configuration](#6-ai--llm-configuration)
7. [Log Sources](#7-log-sources)
8. [Vector Store](#8-vector-store)
9. [Report Format](#9-report-format)
10. [Batch Processing](#10-batch-processing)
11. [Development Guide](#11-development-guide)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Platform Overview

### What It Does

1. **Ingest** — Fetch logs from Splunk (deduped via `eventstats`) or Grafana Loki
2. **Classify** — Fingerprint entries into IncidentPills grouped by error type
3. **Check** — Search pgvector for similar stored reports (≥ 60% composite score); if found, surface a **Similar Reports panel** — AI is never auto-invoked on a cache hit
4. **Analyse** — Answer 15 generic SRE diagnostic questions + 2 unique incident-specific questions using a local LLM (only runs when the user explicitly requests it or no similar reports exist)
5. **Report** — Map all 17 Q&A answers to structured report sections + render 13-section HTML
6. **Stream** — Return every step as Server-Sent Events so the browser updates in real time

### Similar Reports Panel (new UX flow)

When a user clicks **Run RCA** the pipeline always runs steps 1–4 (fetch → classify → pill → vector search). After step 4:

```
vector_check result
  ├── 0 similar reports  ──────────────────────→ llm_analysis → report_assembly → complete(new)
  └── ≥1 report scores ≥60% ──→ complete(similar_found) → STOP (no LLM invoked)
                                       │
                                       ▼
                              "Similar Reports" panel
                              up to 5 cards, each showing:
                                • severity badge + match score %
                                • app name (from registry) + date/time
                                • 1-line summary of what happened
                              ┌─────────────────────────────────┐
                              │ [View Full Report]  per card    │
                              │ [Run New AI Analysis]  (top)    │
                              └─────────────────────────────────┘
                                       │                    │
                                [renders stored]    [POST skip_vector_check:true]
                                HTML inline              │
                                                    llm_analysis → report_assembly
                                                         → complete(new)
```

**`skip_vector_check: true`** can also be sent directly in any `POST /api/rca/process` body to bypass similarity search entirely and force a fresh AI analysis.

### Architecture Diagram

```
Browser (port 8000)
    │  SSE stream  │  REST
    ▼              ▼
┌──────────────────────────────────────────┐
│  api-server  (FastAPI :8000)             │
│  main.py — 30 REST routes                │
│  POST /api/rca/process                   │
│    └── pipeline.graph.stream_rca()       │
│         └── connector.rca_graph          │
│              (LangGraph StateGraph)      │
└──────────┬───────────────────────────────┘
           │ HTTP POST /tools/*
           ▼
┌──────────────────────────────────────────┐
│  mcp-server  (FastAPI :8001)             │
│  10 MCP tools:                           │
│    fetch_logs_splunk / loki              │
│    split_incidents                       │
│    generate_embedding                    │
│    search_similar_rca                    │
│    store_rca_report                      │
│    call_llm                              │
│    scratchpad_*                          │
└──────┬───────────┬────────────┬──────────┘
       │           │            │
       ▼           ▼            ▼
   Splunk       pgvector    LM Studio
   :8089        :5432       :1234
```

### Services

| Service | Port | Description |
|---|---|---|
| `api-server` | 8000 | FastAPI — routes + SSE + LangGraph pipeline |
| `mcp-server` | 8001 | FastAPI — all RCA tool implementations |
| `splunk` | 8000/8089/8088 | Splunk Enterprise log storage |
| `pgvector` | 5432 | PostgreSQL + pgvector for RCA storage |
| `log-generator` | 8090 | Synthetic log generator (1 GB/hr) |
| `app-alpha..epsilon` | 8101-8105 | Demo apps that emit structured errors |
| LM Studio | 1234 | Local LLM server (OpenAI-compatible API) |

---

## 2. Quick Start

### Prerequisites

- Docker + Docker Compose
- LM Studio installed and running at `http://localhost:1234/v1`
- Load two models in LM Studio:
  - **Chat**: `qwen2.5-7b-instruct` (or similar 7B instruction model)
  - **Embed**: `nomic-embed-text-v1.5` (produces 1024-dim vectors)

### Start

```bash
docker compose up -d
```

### URLs

| URL | Service |
|---|---|
| http://localhost:8000 | RCA Dashboard (browser UI) |
| http://localhost:8000/api/health | API health check |
| http://localhost:8001/tools | Available MCP tools |
| http://localhost:8000/docs | FastAPI Swagger UI |
| http://localhost:8000 (splunk port 8000) | Splunk Web UI |

### First RCA

1. Open http://localhost:8000
2. Select an app from the dropdown (e.g. `app-alpha`)
3. Select time window (e.g. `last 1h`)
4. Click **Run RCA**
5. Watch the pipeline stream: fetch → classify → vector check → *(Q&A if no similar reports)*
6. **First run (empty DB):** Full 5-batch Q&A runs → 13-section report renders → click **Accept & Store** to save to pgvector
7. **Subsequent runs (similar reports exist):** Pipeline stops after vector check → **Similar Reports panel** appears with up to 5 matching report cards
   - Each card shows: severity, app name, date/time, 1-line summary, similarity score %
   - Click **View Full Report** on any card to render it inline
   - Click **Run New AI Analysis** to bypass the panel and generate a fresh report

---

## 3. Architecture Deep Dive

### LangGraph Pipeline

The RCA pipeline is a `StateGraph` with 6 typed nodes. All data flows through `RCAState` (a TypedDict in `pipeline/state.py`).

```
fetch_logs → clean_logs → log_pill → vector_check ──┐
                                                      ├──[similar_found] ──→ complete (panel, no LLM)
                                                      ├──[known] ──────────→ report_assembly
                                                      └──[new] ────────────→ llm_analysis → report_assembly
```

Each node:
- Receives the full `RCAState` dict
- Appends `{"step", "status", "data"}` dicts to `state["sse_events"]`
- Returns a partial state update (merged by LangGraph)

`pipeline/graph.py::stream_rca()` calls `connector.rca_graph.astream()` and yields SSE strings as each node completes.

### SSE Event Table

| Step | Status | When |
|---|---|---|
| `fetch_logs` | `running` | Before Splunk query |
| `fetch_logs` | `done` | After receiving entries |
| `clean_logs` | `running` | Before split_incidents |
| `clean_logs` | `done` | After fingerprinting |
| `log_pill` | `running` | Before building pill |
| `log_pill` | `done` | After pill built |
| `vector_check` | `running` | Before embedding + search |
| `vector_check` | `done` | After search result (`similar_count` field = hits ≥ 60%) |
| `llm_analysis` | `running` | Before first Q&A batch (only when no similar reports) |
| `llm_qa` | `running` | Before each 3-question batch |
| `llm_qa` | `done` | After each batch answers arrive |
| `llm_unique_q` | `running` | Before Q16/Q17 generation |
| `llm_unique_q` | `done` | After Q16/Q17 answers arrive |
| `llm_synthesis` | `running` | Before report assembly |
| `llm_synthesis` | `done` | After report dict built |
| `llm_analysis` | `done` | After all AI analysis complete |
| `complete` | `done` | `data.status = "new"` or `"known"` — full report in `data.results` |
| `complete` | `done` | `data.status = "similar_found"` — `data.similar_reports` = top-5 cards, pipeline stops |

### Directory Structure

```
project_root/
│
├── connector.py              ← MASTER CONNECTOR — all toggles
│
├── pipeline/                 ← LangGraph pipeline
│   ├── state.py              ← RCAState TypedDict
│   ├── graph.py              ← StateGraph + stream_rca() SSE generator
│   └── nodes/
│       ├── fetch_logs.py     ← Node 1: fetch from Splunk/Loki
│       ├── clean_logs.py     ← Node 2: split_incidents fingerprinting
│       ├── log_pill.py       ← Node 3: build log pill (pure Python)
│       ├── vector_check.py   ← Node 4: embedding + pgvector search
│       ├── llm_analysis.py   ← Node 5: 15+2 Q&A engine
│       └── report_assembly.py← Node 6: map Q&A → report + render HTML
│
├── ai/                       ← LangChain wrappers
│   ├── llm.py                ← ChatOpenAI chains + PromptTemplates
│   ├── embeddings.py         ← OpenAIEmbeddings wrapper + MCP fallback
│   └── parsers.py            ← QAResponseParser, severity extractor
│
├── api_server/               ← FastAPI HTTP layer
│   ├── main.py               ← ~250 lines: 30 routes + SSE wrapper
│   ├── mcp_client.py         ← Plain HTTP client to mcp-server
│   ├── report_template.py    ← Jinja2 HTML renderer (13 sections)
│   ├── schemas.py            ← Pydantic request/response models
│   ├── pgvector_proxy.py     ← Direct pgvector queries
│   └── requirements.txt      ← Python deps incl. langgraph + langchain
│
├── mcp_server/               ← All tool implementations (unchanged)
├── frontend/                 ← Browser SPA (unchanged)
├── batch_processor/          ← Threaded batch RCA (unchanged)
├── log_generator/            ← Synthetic log generation (unchanged)
└── docker-compose.yml        ← All service definitions (unchanged)
```

---

## 4. connector.py Reference

The master connector file at the project root controls every integration point. Change a flag here and rebuild `api-server` to change runtime behaviour.

```bash
docker compose up -d --build api-server
```

### Section 1 — UI

| Toggle | Default | Effect |
|---|---|---|
| `ENABLE_FRONTEND` | `True` | Serve SPA static files at `/` |
| `FRONTEND_DIR` | `/app/frontend` | Path to static files in container |

### Section 2 — API Server

| Toggle | Default | Effect |
|---|---|---|
| `API_HOST` | `0.0.0.0` | Bind address |
| `API_PORT` | `8000` | FastAPI listen port |
| `ENABLE_BATCH_ENDPOINTS` | `True` | Register `/api/batch/*` routes |
| `ENABLE_ERROR_GEN_ENDPOINTS` | `True` | Register `/api/errors/*` routes |

### Section 3 — MCP Tool Server

| Toggle | Default | Effect |
|---|---|---|
| `ENABLE_MCP` | `True` | Route tool calls to mcp-server |
| `MCP_SERVER_URL` | `http://mcp-server:8001` | mcp-server base URL |
| `MCP_TIMEOUTS` | `{slow:360, fast:30, other:60}` | Per-category HTTP timeouts |

### Section 4 — AI Models

| Toggle | Default | Effect |
|---|---|---|
| `USE_LANGCHAIN_LLM` | `False` | `True` → LangChain ChatOpenAI; `False` → MCP call_llm |
| `USE_LANGCHAIN_EMBEDDINGS` | `False` | `True` → LangChain OpenAIEmbeddings; `False` → MCP |
| `LLM_BASE_URL` | `http://host.docker.internal:1234/v1` | LM Studio / OpenAI endpoint |
| `LLM_CHAT_MODEL` | `local-model` | Model identifier |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature |
| `EMBED_MODEL` | `text-embedding-nomic-embed-text-v1-5` | Embedding model |
| `EMBED_DIMS` | `1024` | Expected vector dimensions |

**Swap LM Studio → OpenAI cloud:**
```python
LLM_BASE_URL   = "https://api.openai.com/v1"
LLM_API_KEY    = "sk-..."
LLM_CHAT_MODEL = "gpt-4o"
USE_LANGCHAIN_LLM = True
```

### Section 5 — Log Sources

| Toggle | Default | Effect |
|---|---|---|
| `LOG_SOURCE` | `"splunk"` | Default source when not specified in request |
| `ENABLE_SPLUNK` | `True` | Allow Splunk fetch |
| `ENABLE_LOKI` | `True` | Allow Loki fetch |
| `LOG_SOURCE_TOOL_MAP` | `{splunk: fetch_logs_splunk, loki: fetch_logs_loki}` | Maps source → MCP tool |

**Swap Splunk → Loki:**
```python
LOG_SOURCE    = "loki"
ENABLE_SPLUNK = False
```

### Section 6 — Vector Store

| Toggle | Default | Effect |
|---|---|---|
| `ENABLE_VECTOR_CHECK` | `True` | Check pgvector before LLM analysis |
| `VECTOR_BACKEND` | `"pgvector"` | `"chroma"` after adding chroma MCP tool |
| `SIMILARITY_THRESHOLD` | `0.60` | Composite score (0–1) at which stored reports are surfaced in the Similar Reports panel; reports below this are ignored |
| `PGVECTOR_DSN` | `postgresql://rca_user:rca_pass@pgvector:5432/rca_db` | DB connection |

`KNOWN_ISSUE_THRESHOLD` is kept as a backward-compat alias pointing to `SIMILARITY_THRESHOLD`.

**Skip vector check globally (always run LLM, never show similar reports panel):**
```python
ENABLE_NODE_VECTOR_CHECK = False
```

**Override threshold at request time (force fresh analysis for one run):**

Send `skip_vector_check: true` in the `POST /api/rca/process` body — bypasses the panel for that single request only.

### Section 7 — Pipeline Nodes

| Toggle | Default | Effect |
|---|---|---|
| `ENABLE_NODE_FETCH_LOGS` | `True` | Include fetch_logs node |
| `ENABLE_NODE_CLEAN_LOGS` | `True` | Include clean_logs node |
| `ENABLE_NODE_LOG_PILL` | `True` | Include log_pill node |
| `ENABLE_NODE_VECTOR_CHECK` | `True` | Include vector_check node |
| `ENABLE_NODE_LLM_ANALYSIS` | `True` | Include llm_analysis node (6 LLM calls) |
| `ENABLE_NODE_REPORT_ASSEMBLY` | `True` | Include report_assembly node |
| `QA_BATCH_SIZE` | `3` | Questions per LLM call (3 = 5 calls for Q01-Q15) |
| `QA_MAX_TOKENS` | `400` | Max tokens per Q&A batch response |

**Disable LLM entirely (vector check only):**
```python
ENABLE_NODE_LLM_ANALYSIS    = False
ENABLE_NODE_REPORT_ASSEMBLY = False
```

### Section 8 — Report Output

| Toggle | Default | Effect |
|---|---|---|
| `ENABLE_HTML_REPORT` | `True` | Render 13-section Jinja2 HTML; `False` = JSON-only |

### Section 9 — Batch Processing

| Toggle | Default | Effect |
|---|---|---|
| `BATCH_MAX_CHUNKS` | `50` | Max log chunks per batch job |
| `BATCH_MAX_THREADS` | `8` | Worker threads for parallel chunk processing |

### Section 10 — LangGraph Graph

`build_rca_graph()` assembles the StateGraph at import time reading all flags above. The compiled graph is available as `connector.rca_graph`.

Conditional edge logic after `vector_check`:
- `has_similar_reports=True` → `graph.py` emits `complete(similar_found)` and returns early (LLM never invoked)
- `is_known=True` → skip `llm_analysis` → go directly to `report_assembly`
- `is_known=False` (default) → run `llm_analysis` → `report_assembly`
- `ENABLE_NODE_VECTOR_CHECK=False` → direct `log_pill → llm_analysis` edge (no vector search at all)
- `skip_vector_check=True` (per-request flag) → `vector_check_node` returns immediately with `has_similar_reports=False`, pipeline continues to LLM

---

## 5. MCP Tool Reference

### POST /api/rca/process — Request Body

```json
{
  "app_id":             "app-alpha",
  "since_seconds":      3600,
  "source":             "splunk",
  "skip_vector_check":  false
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `app_id` | string | required | Application identifier (matches Splunk index label) |
| `since_seconds` | int | `3600` | Look-back window (60 – 86400) |
| `source` | string | `"splunk"` | Log source: `"splunk"` or `"loki"` |
| `skip_vector_check` | bool | `false` | `true` → bypass similarity search and run full AI analysis regardless of stored reports |

### MCP Tools

All tools are registered in `mcp_server/server.py` and callable at `POST /tools/{tool_name}`.

| Tool | Inputs | Output | Used By |
|---|---|---|---|
| `fetch_logs_splunk` | `app_id, start_time, end_time, max_events` | `{entries, total_raw_lines}` | fetch_logs node |
| `fetch_logs_loki` | `app_id, start_time, end_time, max_events` | `{entries, total_raw_lines}` | fetch_logs node |
| `split_incidents` | `entries, app_id` | `{incidents}` — IncidentPills | clean_logs node |
| `generate_embedding` | `text` | `{embedding}` — list[float] | vector_check node |
| `search_similar_rca` | `incident_text, embedding, app_id, incident_category, incident_error_type, incident_pill_text` | `{has_similar_reports, similar_reports[top-5], is_known_issue, hits, best_distance, best_score}` | vector_check node |
| `store_rca_report` | `report, embedding, app_id, embed_source` | `{id, stored}` | `/api/rca/accept` route |
| `call_llm` | `prompt, system, max_tokens, is_reasoning` | `{content}` | llm_analysis node |
| `scratchpad_write` | `key, value` | `{ok}` | (available for future use) |
| `scratchpad_read` | `key` | `{value}` | (available for future use) |
| `clean_logs` | `entries` | `{entries}` — cleaned | (optional pre-processing) |

---

## 6. AI / LLM Configuration

### LM Studio Setup

1. Download and install [LM Studio](https://lmstudio.ai)
2. Load a chat model: recommended `Qwen2.5-7B-Instruct` or `Llama-3.1-8B-Instruct`
3. Load an embedding model: recommended `nomic-embed-text-v1.5` (1024 dims)
4. Start the local server at `http://localhost:1234/v1`
5. Both models must be loaded simultaneously (LM Studio supports dual-model serving)

### Environment Variables

Set in `docker-compose.yml` under `api-server.environment`:

```yaml
LLM_BASE_URL: "http://host.docker.internal:1234/v1"
LLM_API_KEY: "lm-studio"
LLM_CHAT_MODEL: "qwen2.5-7b-instruct"
EMBED_MODEL: "text-embedding-nomic-embed-text-v1-5"
EMBED_DIMS: "1024"
```

### LangChain vs MCP LLM Calls

By default `USE_LANGCHAIN_LLM = False` — the pipeline calls the `call_llm` MCP tool which is implemented in `mcp_server/server.py` and makes a plain HTTP request to the LLM endpoint. This is simpler, more portable, and doesn't require LangChain to be installed.

Setting `USE_LANGCHAIN_LLM = True` in `connector.py` routes Q&A calls through `ai/llm.py::build_qa_chain()` which uses LangChain's `ChatOpenAI | PromptTemplate | StrOutputParser` chain. Both paths produce identical output — the LangChain path is useful if you want LCEL middleware (retry logic, callbacks, tracing with LangSmith).

### 15-Question Q&A Engine

The LLM is called 6 times per RCA run:
- **5 calls** — Q01-Q15 in batches of 3 (`QA_BATCH_SIZE`)
- **1 call** — Q16-Q17 unique incident-specific questions

Each call uses the same compact `pill_header` as context (app name, window, error count, top types). The `QAResponseParser` (`ai/parsers.py`) splits the response on `Q\d+:` markers.

---

## 7. Log Sources

### Splunk (Default)

Splunk deduplicates at query time using `eventstats count by fingerprint`. This means:
- The mcp-server returns ≤1000 pre-aggregated pattern rows regardless of index size
- `total_raw_lines` is the true event count from `eventstats`
- `entries` is the deduplicated list of unique patterns with counts

Query uses `spath`, `rex`, and `eventstats` to extract and fingerprint error patterns before returning them to the api-server.

### Loki

Loki fetches use the Loki HTTP API (`/loki/api/v1/query_range`). Results are passed through `split_incidents` for the same fingerprinting step as Splunk.

**To switch to Loki:**
```python
# connector.py
LOG_SOURCE    = "loki"
ENABLE_SPLUNK = False
```
Also set `LOKI_URL` in `docker-compose.yml`.

### Adding a New Log Source

1. Implement a new MCP tool in `mcp_server/server.py` (e.g. `fetch_logs_elastic`)
2. Add to `connector.py` `LOG_SOURCE_TOOL_MAP`:
   ```python
   LOG_SOURCE_TOOL_MAP["elastic"] = "fetch_logs_elastic"
   ```
3. Set `LOG_SOURCE = "elastic"` in `connector.py`

---

## 8. Vector Store

### pgvector Schema

```sql
CREATE TABLE rca_reports (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_id      TEXT,
    category    TEXT,
    error_type  TEXT,
    pill_text   TEXT,
    report      JSONB,
    embedding   vector(1024),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
```

### 4-Stage Composite Scoring

`search_similar_rca` scores every stored report against the current incident using four weighted signals:

| Stage | Weight | Metric |
|---|---|---|
| Category match | 0.15 | Exact match on `category` field (e.g. `"database"`, `"auth"`) |
| Error type match | 0.25 | Exact match on primary `error_type` (e.g. `"DB_CONN"`) |
| Pill text overlap | 0.25 | Jaccard similarity of error-type token sets from `pill_text` |
| Cosine distance | 0.35 | pgvector `<=>` operator on 1024-dim embeddings |

Max possible score = 1.0 (all four stages match perfectly).

**Similarity threshold** (`SIMILARITY_THRESHOLD`, default `0.60`):
- Reports scoring **≥ 60%** are included in the Similar Reports panel (top 5, ranked by score)
- Reports scoring **< 60%** are ignored — pipeline continues to LLM analysis

Each hit in `similar_reports` carries:
```json
{
  "id":             "<uuid>",
  "app_id":         "app-alpha",
  "error_type":     "DB_CONN",
  "severity":       "high",
  "summary":        "Database connection pool exhausted under peak load.",
  "similarity_pct": 87.3,
  "score":          0.873,
  "cosine_distance": 0.14,
  "created_at":     "2026-05-12T14:32:10.000Z",
  "report":         { ... }
}
```

### Storage Field Extraction

Reports are stored with `error_type` and `category` extracted from the top-level fields of the Q&A-format report dict (`report["error_type"]`, `report["category"]`). These are written by `report_assembly_node` before the report reaches `/api/rca/accept`. Both fields feed directly into composite scoring — correct values are critical for accurate similarity ranking.

### Resetting the Vector Store

```bash
curl -X POST http://localhost:8000/api/vectordb/reset \
  -H "Content-Type: application/json" \
  -d '{"app_id": "app-alpha"}'   # omit app_id to reset all
```

---

## 9. Report Format

The generated report has 13 sections. Each section maps from Q&A answers:

| # | Section | Source Questions |
|---|---|---|
| 1 | Executive Summary | log_pill metadata + severity (Q13) |
| 2 | Problem Statement | Q01 (primary component) + Q03 (trigger) |
| 3 | Blast Radius | Q10 |
| 4 | Root Cause Analysis | Q02 |
| 5 | Contributing Factors | Q06, Q07, Q08, Q09, Q12 |
| 6 | Timeline of Events | Q04 (split into ordered steps) |
| 7 | Causal Chain | Q11 (arrow-parsed into chain nodes) |
| 8 | Recommended Fix Steps | Q14 (split into steps) |
| 9 | Long-term Prevention | Q15 (split into steps) |
| 10 | Verification Plan | Derived from top error type + Q14/Q15 |
| 11 | Unique Insights | Q16 + Q17 (purple badge, AI-generated) |
| 12 | Error Breakdown | log_pill top_errors table |
| 13 | Full Q&A Reference | All 17 Q&A pairs (collapsible) |

---

## 10. Batch Processing

```bash
curl -X POST http://localhost:8000/api/batch/process \
  -H "Content-Type: application/json" \
  -d '{"app_id": "app-alpha", "since_seconds": 3600, "threads": 4, "source": "splunk"}'
```

Check status:
```bash
curl http://localhost:8000/api/batch/status?job_id=batch-app-alpha-1710000000
```

Configuration in `connector.py`:
- `BATCH_MAX_THREADS` — parallel worker threads (default 8)
- `BATCH_MAX_CHUNKS` — maximum log chunks per job (default 50)
- `LOG_CHUNK_SIZE` — lines per chunk (default 500, from `LOG_CHUNK_SIZE` env var)

---

## 11. Development Guide

### Adding a New Pipeline Node

1. Create `pipeline/nodes/my_node.py`:
   ```python
   async def my_node(state: dict) -> dict:
       events = list(state.get("sse_events", []))
       events.append({"step": "my_step", "status": "running", "data": "..."})
       # ... your logic ...
       events.append({"step": "my_step", "status": "done", "data": {...}})
       return {"my_output_field": result, "sse_events": events}
   ```

2. Add a flag to `connector.py` Section 7:
   ```python
   ENABLE_NODE_MY_NODE = True
   ```

3. Add to `node_registry` in `connector.py::build_rca_graph()`:
   ```python
   ("my_node", my_node_fn, ENABLE_NODE_MY_NODE),
   ```

4. Add to `RCAState` in `pipeline/state.py`:
   ```python
   my_output_field: str
   ```

5. Rebuild: `docker compose up -d --build api-server`

### Adding a New MCP Tool

1. Add to `mcp_server/server.py`:
   ```python
   @app.post("/tools/my_tool")
   async def my_tool(body: dict):
       # implementation
       return {"result": ...}
   ```

2. Add to `mcp_client.py` timeout buckets if needed (Section 3 of connector)

3. Rebuild: `docker compose up -d --build mcp-server`

### Running Without Docker

```bash
# Terminal 1 — mcp-server
cd mcp_server
pip install -r requirements.txt
uvicorn server:app --port 8001 --reload

# Terminal 2 — api-server
cd api_server
pip install -r requirements.txt
MCP_SERVER_URL=http://localhost:8001 uvicorn main:app --port 8000 --reload
```

Set `FRONTEND_DIR` to the absolute path of the `frontend/` folder.

---

## 12. Troubleshooting

### LLM Not Responding / Pipeline Stuck

**Symptoms:** `llm_analysis` running event appears but never resolves.

**Checks:**
1. Is LM Studio running? `curl http://localhost:1234/v1/models`
2. Is the chat model loaded? Check LM Studio model selector.
3. Check api-server logs: `docker compose logs api-server -f`
4. Check mcp-server logs: `docker compose logs mcp-server -f`

**Fix:** Restart LM Studio, reload the model, then retry.

### Splunk Returns 0 Events

**Symptoms:** `fetch_logs` done with `count: 0`.

**Checks:**
1. Is the Splunk index `rca_logs` populated? Check http://localhost:8000 (Splunk Web)
2. Is the log generator running? `curl http://localhost:8000/api/log-generator/status`
3. Is the time window correct? Try `since_seconds=86400` (24h)
4. Check `SPLUNK_INDEX`, `SPLUNK_REST_URL`, `SPLUNK_PASSWORD` env vars in `docker-compose.yml`

### pgvector Errors (store / search failing)

**Symptoms:** `rca_accept` returns 500 or `search_similar_rca` returns empty.

**Checks:**
1. `docker compose logs pgvector -f` — check for init errors
2. `docker compose exec pgvector psql -U rca_user -d rca_db -c "\dt"` — verify schema
3. Check `PGVECTOR_DSN` in connector or env vars

**Reset:** `curl -X POST http://localhost:8000/api/vectordb/reset -d '{}'`

### SSE Stream Drops / Reconnects

**Symptoms:** Browser console shows EventSource reconnects; pipeline restarts from Step 1.

**Fix:** The browser EventSource automatically reconnects. This is normal for long-running LLM calls (>60s). The pipeline will re-run from scratch on reconnect because each POST /api/rca/process is stateless.

For stability: reduce the log window (`since_seconds`), or reduce `QA_BATCH_SIZE` to 2 (more calls, shorter each).

### Container Won't Build

**Symptoms:** `api-server` build fails with import error.

**Common causes:**
- `langgraph` not in `requirements.txt` → already added in current version
- Python package version conflict → check `requirements.txt` for `>=` constraints

**Fix:**
```bash
docker compose build --no-cache api-server
docker compose up -d api-server
```

### Embedding Dimension Mismatch

**Symptoms:** Reports stored but "Accept & Store" shows ✅ but nothing appears in similar reports.

**Root cause:** The embed model produces N dims but pgvector was created with VECTOR(M). Every insert silently fails.

**Diagnosis:** Run `run tests.bat` → Test 2 (LLM Embedding) will show the actual dims and warn if they differ from `EMBED_DIMS` in `.env`.

**Fix:**
1. Note the real dimension from Test 2 (e.g. 768)
2. Update `.env`: `EMBED_DIMS=768`
3. Restart mcp-server: `docker compose up -d mcp-server`
4. The server auto-detects the mismatch and drops/recreates the table on startup (logged as `WARNING: Embedding dimension mismatch`)

---

## 13. Component Tests

A self-contained test script checks every integration point and prints a colour-coded pass/fail report.

### Run

```bat
run tests.bat
```

Or directly:
```bash
python test_components.py
```

### What It Tests

| # | Test | What It Checks | Pass Condition |
|---|---|---|---|
| 1 | LLM Chat | `POST /v1/chat/completions` to LM Studio | HTTP 200 + non-empty response |
| 2 | LLM Embedding | `POST /v1/embeddings` | HTTP 200 + vector length == `EMBED_DIMS` |
| 3 | Splunk REST Auth | `GET /services/server/info` with admin credentials | HTTP 200 + Splunk version |
| 4 | Splunk Search | Submit SPL job, poll to completion, fetch 5 events | ≥1 event returned from `SPLUNK_INDEX` |
| 5 | Splunk HEC | `POST` test event via HEC token | `{"text":"Success"}` |
| 6 | pgvector DB | `SELECT COUNT(*) FROM rca_reports` | Connection succeeds |
| 7 | MCP Server | `GET /health` + `GET /tools` | HTTP 200 + tools listed |
| 8 | API Server | `GET /api/health` + `GET /api/apps` | HTTP 200 + app registry accessible |
| 9 | Vector Round-trip | `generate_embedding` → `search_similar_rca` via MCP | Embedding generated, search returns |

### Interpreting Results

```
──────────────────────────────────────────────────────────
  1 · LLM Chat  (LM Studio)
  URL   : http://localhost:1234/v1
  Model : meta-llama-3.1-8b-instruct@q5_k_m
  ✓ PASS  LLM Chat  HTTP 200 | response: 'OK'

  2 · LLM Embedding  (LM Studio)
  ✗ FAIL  LLM Embedding  Got 768 dims but EMBED_DIMS=1024 — update EMBED_DIMS in .env to 768
```

If **LLM Chat fails** → LM Studio is not running or the chat model is not loaded.  
If **LLM Embedding fails with wrong dims** → update `EMBED_DIMS` in `.env` to match the reported value.  
If **Splunk Search fails with 0 events** → log generator is not running or `SPLUNK_INDEX` is wrong.  
If **MCP Server fails** → check `docker compose logs mcp-server`.  
If **Vector Round-trip fails** → LLM embedding model is not available to the MCP container.

---

## 14. Splunk Configuration Guide

### How Splunk Is Integrated

The RCA platform talks to Splunk via two interfaces:

| Interface | Port | Purpose |
|---|---|---|
| HEC (HTTP Event Collector) | 8088 | Log generators write events into Splunk |
| REST API | 8089 | MCP server fetches logs with SPL search |

The MCP tool `fetch_logs_splunk` submits a search job via REST, polls until done, and returns deduplicated error patterns (up to 1000 rows).

### SPL Query Used

```spl
index=<SPLUNK_INDEX> app_id=<app_id>
    earliest=-<since_seconds>s latest=now
| rex field=_raw "(?i)(?:error|exception|fail)[^\n]*"
| eval fingerprint=md5(_raw)
| eventstats count by fingerprint
| dedup fingerprint
| sort -count
| head 1000
| table _time, _raw, count, fingerprint
```

The `eventstats + dedup` pattern deduplicates at Splunk query time so the MCP server always receives pre-aggregated unique patterns regardless of index volume.

### Default Index: `rca_logs`

The `rca_logs` index is created automatically on first start by the Splunk app mounted at `infra/splunk/rca_app/`.

**Index config** (`infra/splunk/rca_app/local/indexes.conf`):
```ini
[rca_logs]
homePath   = $SPLUNK_DB/rca_logs/db
coldPath   = $SPLUNK_DB/rca_logs/colddb
thawedPath = $SPLUNK_DB/rca_logs/thaweddb
maxTotalDataSizeMB = 10240
frozenTimePeriodInSecs = 604800    ; 7 days retention
```

### Adding a New Splunk Index

1. Edit `infra/splunk/rca_app/local/indexes.conf` — add a new stanza:
   ```ini
   [my_new_index]
   homePath   = $SPLUNK_DB/my_new_index/db
   coldPath   = $SPLUNK_DB/my_new_index/colddb
   thawedPath = $SPLUNK_DB/my_new_index/thaweddb
   maxTotalDataSizeMB = 5120
   frozenTimePeriodInSecs = 604800
   ```

2. Restart Splunk to apply:
   ```bash
   docker compose restart splunk
   ```

3. Update `.env` to use the new index:
   ```
   SPLUNK_INDEX=my_new_index
   ```

4. Or keep `rca_logs` as default and override per-app (see "Configuring a New App" below).

### Allowing a HEC Token to Write to Additional Indexes

Edit `infra/splunk/rca_app/local/inputs.conf` — add the new index to `indexes`:
```ini
[http://rca_hec]
disabled = 0
token    = rca-hec-token-00000000-0000-0000-0000-000000000001
index    = rca_logs
indexes  = rca_logs,my_new_index
sourcetype = rca_app_log
```

Restart Splunk after editing:
```bash
docker compose restart splunk
```

### Creating a New Splunk Token for a Different Team/Index

1. Log into Splunk Web at http://localhost:8080 (admin / changeme)
2. Go to **Settings → Data Inputs → HTTP Event Collector**
3. Click **New Token** → name it → set **Default Index** → save
4. Copy the token value
5. Update `.env`:
   ```
   SPLUNK_HEC_TOKEN=<new-token>
   ```
6. Restart affected containers:
   ```bash
   docker compose up -d mcp-server log-generator
   ```

---

## 15. Adding and Configuring a New App

An "app" in the RCA platform is any service whose logs flow through Splunk. Each app has its own lane in the vector store and its own RCA history.

### Step 1 — Register the App

**Via the UI:**
1. Open http://localhost:8000
2. The app dropdown is populated from the registry — if your app isn't listed, register it first
3. *(App registration UI coming — use the API for now)*

**Via the API:**
```bash
curl -X POST http://localhost:8000/api/apps \
  -H "Content-Type: application/json" \
  -d '{
    "app_id":          "my-service",
    "app_name":        "My Service",
    "service_name":    "my-service",
    "port":            8080,
    "container_name":  "rca-my-service",
    "vector_category": "my-service",
    "enabled_sources": ["splunk"]
  }'
```

| Field | Required | Description |
|---|---|---|
| `app_id` | ✓ | Unique ID — must match the `app_id` field written into Splunk logs |
| `app_name` | ✓ | Human-readable name shown in the UI |
| `service_name` | ✓ | Docker service name or hostname |
| `port` | — | HTTP port of the service (for health checks) |
| `container_name` | — | Docker container name |
| `vector_category` | — | Namespace for vector similarity search (default: `"default"`) |
| `enabled_sources` | — | `["splunk"]`, `["loki"]`, or both |

### Step 2 — Make Your App Write Logs with `app_id`

The RCA platform identifies logs by the `app_id` field. Your application logs must include this tag so Splunk can filter them.

**Using Splunk HEC directly from your app:**
```python
import requests

hec_url   = "http://localhost:8088/services/collector/event"
hec_token = "rca-hec-token-00000000-0000-0000-0000-000000000001"

requests.post(hec_url,
    headers={"Authorization": f"Splunk {hec_token}"},
    json={
        "index":      "rca_logs",
        "sourcetype": "rca_app_log",
        "event": {
            "app_id":    "my-service",
            "level":     "ERROR",
            "message":   "DB_CONN: Connection refused to db-host:5432",
            "timestamp": "2026-05-17T10:00:00Z",
        }
    }
)
```

**Using the log generator pattern** (see `log_generator/` for a full example): set `APP_ID=my-service` in the container's environment and the generator handles HEC delivery.

**Required log field:** `app_id` must appear somewhere in the raw log line (as JSON key or plain text) for the Splunk SPL query to filter correctly. The query uses `app_id=<value>` as a field filter.

### Step 3 — Run RCA for the New App

1. Open http://localhost:8000
2. Select **My Service** from the app dropdown
3. Select time window and click **Run RCA**

If the app has no logs yet, `fetch_logs` will return 0 events — check that:
- The log generator or your app is writing to `SPLUNK_INDEX` with the correct `app_id`
- The time window covers the log activity period

### Step 4 — Verify Logs in Splunk

Open Splunk Web → Search:
```spl
index=rca_logs app_id=my-service | head 20
```

If nothing returns: logs aren't reaching Splunk. Check HEC connectivity with Test 5 from `run tests.bat`.

### App Registry Storage

Apps are stored in the `apps` table in pgvector (PostgreSQL):
```sql
SELECT app_id, app_name, service_name, enabled_sources, created_at
FROM apps
ORDER BY created_at DESC;
```

To delete an app:
```bash
curl -X DELETE http://localhost:8000/api/apps/my-service
```

This does **not** delete the app's stored RCA reports. To also clear reports:
```bash
curl -X POST http://localhost:8000/api/vectordb/reset \
  -H "Content-Type: application/json" \
  -d '{"app_id": "my-service"}'
```

---

## 16. How the System Works — Detailed Flow

### Full Pipeline: Request to Report

```
Browser                 api-server              mcp-server          Splunk / pgvector / LLM
  │                         │                       │                        │
  │── POST /api/rca/process ─►                      │                        │
  │   {app_id, since_sec,   │                       │                        │
  │    source, skip_vec}    │                       │                        │
  │                         │── stream_rca() ───────────────────────────────►│
  │                         │                       │                        │
  │◄── SSE: fetch_logs ─────│                       │                        │
  │    running              │── fetch_logs_splunk ──►                        │
  │                         │                       │── SPL search ─────────►│
  │                         │                       │◄── events (deduped) ───│
  │◄── SSE: fetch_logs ─────│                       │                        │
  │    done (N lines)       │                       │                        │
  │                         │── split_incidents ────►                        │
  │                         │                       │ fingerprint + group    │
  │◄── SSE: clean_logs ─────│                       │                        │
  │    done (M patterns)    │                       │                        │
  │                         │ log_pill_node         │                        │
  │◄── SSE: log_pill ───────│ (pure Python —        │                        │
  │    done                 │  no MCP call)         │                        │
  │                         │                       │                        │
  │                         │── generate_embedding ─►                        │
  │                         │                       │── POST /v1/embeddings ►│
  │                         │                       │◄── 768-dim vector ─────│
  │                         │── search_similar_rca ─►                        │
  │                         │                       │── pgvector <=> query ──►│
  │                         │                       │◄── top-N hits ─────────│
  │                         │                       │ 4-stage re-rank        │
  │◄── SSE: vector_check ───│                       │                        │
  │    done                 │                       │                        │
  │                         │                       │                        │
  │  ┌─── if similar_found ─┤                       │                        │
  │◄─┤ SSE: complete        │                       │                        │
  │  │ status:similar_found │                       │                        │
  │  │ similar_reports:[..]  │                       │                        │
  │  └──────────────────────┤                       │                        │
  │   → show panel, STOP    │                       │                        │
  │                         │                       │                        │
  │  ┌─── if no similar ────┤                       │                        │
  │  │ llm_analysis node    │                       │                        │
  │  │ (5 batches × 3 Q)    │── call_llm ×6 ────────►                        │
  │  │                      │                       │── POST /v1/chat ───────►│
  │  │                      │                       │◄── answers ────────────│
  │◄─┤ SSE: llm_qa ×5       │                       │                        │
  │  │ SSE: llm_unique_q    │                       │                        │
  │  │                      │                       │                        │
  │  │ report_assembly node │                       │                        │
  │  │ (pure Python)        │                       │                        │
  │◄─┤ SSE: complete        │                       │                        │
  │  │ status:new           │                       │                        │
  │  │ report:{13 sections} │                       │                        │
  │  └──────────────────────┤                       │                        │
  │                         │                       │                        │
  │── Accept & Store ───────►                        │                        │
  │   POST /api/rca/accept  │── store_rca_report ───►                        │
  │                         │                       │── INSERT rca_reports ──►│
  │◄── {id, stored:true} ───│                       │◄── UUID ───────────────│
```

### State Object (RCAState)

Every piece of data flows through a single TypedDict — `pipeline/state.py`:

| Field | Set by | Contains |
|---|---|---|
| `app_id`, `since_seconds`, `source`, `skip_vector_check` | `stream_rca()` | Request inputs |
| `entries`, `total_raw_lines` | `fetch_logs_node` | Raw Splunk events |
| `all_incidents` | `clean_logs_node` | Fingerprinted IncidentPills |
| `log_pill`, `top_errors`, `pill_text`, `window_str` | `log_pill_node` | Compact error summary |
| `embedding` | `vector_check_node` | 768-dim float list (always generated) |
| `has_similar_reports`, `similar_reports` | `vector_check_node` | Similarity search results |
| `answers`, `unique_qa` | `llm_analysis_node` | Q01-Q17 answers |
| `report`, `html` | `report_assembly_node` | Final structured report + rendered HTML |

### Composite Similarity Scoring

When a new incident is analysed, every stored report for the same `app_id` is scored against it:

```
score = 0.15 × (category_match)        # "database-connection" == "database-connection" → 1.0
      + 0.25 × (error_type_match)       # "DB_CONN" == "DB_CONN" → 1.0
      + 0.25 × (jaccard_pill_overlap)   # word overlap between pill_text and stored report
      + 0.35 × (1 − cosine_distance)    # embedding similarity (0.35 weight = most important)
```

A score ≥ 0.60 (60%) surfaces the report in the Similar Reports panel. The top 5 are shown.

**Why 4 stages?** Cosine distance alone can match reports from different error types that happen to use similar vocabulary. The category + error type exact matches act as hard filters that penalise cross-type confusion.

### Embedding Generation

- Model: `nomic-embed-text-v1.5` loaded in LM Studio → 768-dim vectors
- Input to embed: `pill_text` = concatenation of top-15 error types + their sample messages
- Always generated even when `skip_vector_check=True` (needed for storage)
- Stored in pgvector as `VECTOR(768)` with HNSW index (`m=16`, `ef_construction=128`)

### Why the Pipeline Stops at `similar_found`

When ≥1 stored report scores ≥60%, the pipeline emits `complete(status:similar_found)` and returns **without calling `llm_analysis`**. This is intentional:

- LLM calls take 30–120 seconds and cost tokens
- If a stored report already describes the same root cause, showing it first saves time
- The user retains full control: "View Full Report" shows the stored analysis, "Run New AI Analysis" bypasses the panel and forces a fresh LLM run

### Report Storage

When the user clicks **Accept & Store**:
1. The frontend POSTs `{report, embedding, app_id}` to `POST /api/rca/accept`
2. api-server strips `log_pill` (large, re-derivable) but keeps `html` (needed for "View Full Report")
3. mcp-server calls `pgdb.insert_report()` which extracts `error_type` and `category` from the report dict and inserts into `rca_reports`
4. The stored row: `(id, app_id, error_type, category, report JSONB, embedding VECTOR(768))`
5. The HNSW index is updated automatically by pgvector
