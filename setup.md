# RCA Platform — Setup & Configuration Guide

Quick reference for configuring Splunk, LLM, and all other integration points.

---

## Table of Contents

1. [The One File That Controls Everything](#1-the-one-file-that-controls-everything)
2. [Splunk Configuration](#2-splunk-configuration)
3. [LLM Configuration](#3-llm-configuration)
4. [What to Do After Changing Each Setting](#4-what-to-do-after-changing-each-setting)
5. [Where Settings Flow in the Code](#5-where-settings-flow-in-the-code)
6. [Starting the Platform](#6-starting-the-platform)

---

## 1. The One File That Controls Everything

**`.env`** in the project root is the single source of truth for all configuration.

Every service — the MCP server, the API server, the log generator, the demo apps — reads from this file.
If you change `.env`, the change takes effect after restarting the relevant service (see [Section 4](#4-what-to-do-after-changing-each-setting)).

```
project root/
├── .env                  ← EDIT THIS for any config change
├── start mcp.bat         ← reads .env + sets localhost overrides
├── start api.bat         ← reads .env + sets localhost overrides
└── docker-compose.yml    ← passes .env values into Docker containers
```

---

## 2. Splunk Configuration

### 2a. Connection Details (where to edit)

**File: `.env`**

```ini
# Splunk HEC — used to WRITE logs into Splunk
SPLUNK_HEC_URL=http://localhost:8088/services/collector/event
SPLUNK_HEC_TOKEN=rca-hec-token-00000000-0000-0000-0000-000000000001

# Splunk REST API — used to SEARCH/QUERY logs
SPLUNK_REST_URL=https://localhost:8089
SPLUNK_PASSWORD=changeme

# Index where logs are stored and searched
SPLUNK_INDEX=rca_logs
```

> **Changing just `.env` is enough for URL, password, and index name.**
> Changing the HEC token also requires updating two Splunk config files (see 2b).

---

### 2b. HEC Token — Requires Updating 3 Places

The HEC token must match in all three locations:

| # | File | What to change |
|---|---|---|
| 1 | `.env` | `SPLUNK_HEC_TOKEN=<your-new-token>` |
| 2 | `infra/splunk/inputs.conf` | `token = <your-new-token>` under `[http://rca_hec]` |
| 3 | `infra/splunk/rca_app/local/inputs.conf` | same — `token = <your-new-token>` under `[http://rca_hec]` |

After editing all three: **restart the Splunk container**:
```bat
docker compose restart splunk
```

The `.conf` files are mounted into the Splunk container at startup. Splunk reads them on start — a restart is required.

---

### 2c. Splunk Index — Requires Updating 3 Places

If you want to use a different index (e.g. `my_app_logs` instead of `rca_logs`):

| # | File | What to change |
|---|---|---|
| 1 | `.env` | `SPLUNK_INDEX=my_app_logs` |
| 2 | `infra/splunk/indexes.conf` | Rename `[rca_logs]` → `[my_app_logs]` |
| 3 | `infra/splunk/rca_app/local/indexes.conf` | Same rename |

Then restart Splunk:
```bat
docker compose restart splunk
```

---

### 2d. Splunk Infrastructure Files (what they do)

These files live in `infra/splunk/` and are mounted into the Splunk container:

```
infra/splunk/
├── inputs.conf                        ← enables HEC, sets token, port 8088
├── indexes.conf                       ← defines rca_logs index (30-day retention)
└── rca_app/
    └── local/
        ├── inputs.conf                ← app-level copy of HEC config (same token)
        ├── indexes.conf               ← app-level copy of index (7-day retention)
        └── server.conf                ← allows REST API remote login (dev only)
```

**You do not need to edit these files** unless you are changing the HEC token or index name.
They are pre-configured to work out of the box.

---

### 2e. Pointing to an External Splunk (not the Docker one)

If you have a real Splunk server instead of the Docker container:

1. Update `.env`:
   ```ini
   SPLUNK_HEC_URL=https://your-splunk-host:8088/services/collector/event
   SPLUNK_HEC_TOKEN=<token from your Splunk admin>
   SPLUNK_REST_URL=https://your-splunk-host:8089
   SPLUNK_PASSWORD=<your admin password>
   SPLUNK_INDEX=<your index name>
   ```

2. Remove (or comment out) the `splunk` service from `docker-compose.yml` — you no longer need the local container.

3. Restart MCP server (`start mcp.bat`).

No changes needed to infra config files — those only apply to the local Docker Splunk container.

---

## 3. LLM Configuration

### 3a. Connection Details (where to edit)

**File: `.env`**

```ini
# Where LLM is running (OpenAI-compatible endpoint)
LLM_BASE_URL=http://host.docker.internal:1234/v1

# API key — any non-empty string for LM Studio; real key for OpenAI cloud
LLM_API_KEY=lm-studio

# Chat model — LM Studio uses the filename; OpenAI uses the model ID
LLM_CHAT_MODEL=meta-llama-3.1-8b-instruct@q5_k_m

# Embedding model
EMBED_MODEL=text-embedding-nomic-embed-text-v1.5

# Dimensions the embedding model outputs — must match what the model actually returns
# nomic-embed-text-v1.5 → 768   |   text-embedding-3-small → 1536
EMBED_DIMS=768
```

Changing `.env` is all that's needed. Restart the MCP server after.

---

### 3b. Switching LLM Providers

**LM Studio (local, default):**
```ini
LLM_BASE_URL=http://localhost:1234/v1
LLM_API_KEY=lm-studio
LLM_CHAT_MODEL=meta-llama-3.1-8b-instruct@q5_k_m
```

**OpenAI cloud:**
```ini
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_CHAT_MODEL=gpt-4o
EMBED_MODEL=text-embedding-3-small
EMBED_DIMS=1536
```

**Ollama (local):**
```ini
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_CHAT_MODEL=llama3.1
```

**Any OpenAI-compatible API** — just change the base URL and model name.

---

### 3c. Important: EMBED_DIMS Must Match the Model

If `EMBED_DIMS` doesn't match what the model actually returns, every report store will fail silently.

The platform auto-detects a mismatch on MCP server startup and drops/recreates the `rca_reports` table automatically — but this **deletes all stored reports**.

To avoid data loss: always set `EMBED_DIMS` correctly before storing any reports.

| Model | Correct EMBED_DIMS |
|---|---|
| `nomic-embed-text-v1.5` (LM Studio) | `768` |
| `text-embedding-3-small` (OpenAI) | `1536` |
| `text-embedding-3-large` (OpenAI) | `3072` |
| `text-embedding-ada-002` (OpenAI) | `1536` |

---

### 3d. Where the LLM Connection Lives in Code

The MCP server is the only component that directly calls the LLM.
The API server never calls the LLM directly — it goes through the MCP server.

```
start api.bat → api_server/main.py
                   ↓ calls MCP tool
                start mcp.bat → mcp_server/llm_client.py
                                   ↓ calls OpenAI-compatible endpoint
                                LM Studio / OpenAI / Ollama
```

Config is read once at import time from `mcp_server/llm_client.py`:
```python
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://host.docker.internal:1234/v1")
CHAT_MODEL   = os.getenv("LLM_CHAT_MODEL", "meta-llama-3.1-8b-instruct@q3_k_l")
EMBED_MODEL  = os.getenv("EMBED_MODEL",  "text-embedding-nomic-embed-text-v1.5")
EMBED_DIMS   = int(os.getenv("EMBED_DIMS", "1024"))
```

**After changing any LLM setting: restart `start mcp.bat`** (Ctrl+C, run again).

---

## 4. What to Do After Changing Each Setting

| Setting changed | Files to edit | Action required |
|---|---|---|
| **Splunk HEC URL** | `.env` | Restart `start mcp.bat` |
| **Splunk REST URL** | `.env` | Restart `start mcp.bat` |
| **Splunk password** | `.env` | Restart `start mcp.bat` |
| **Splunk HEC token** | `.env` + `infra/splunk/inputs.conf` + `infra/splunk/rca_app/local/inputs.conf` | `docker compose restart splunk` + restart `start mcp.bat` |
| **Splunk index name** | `.env` + `infra/splunk/indexes.conf` + `infra/splunk/rca_app/local/indexes.conf` | `docker compose restart splunk` + restart `start mcp.bat` |
| **LLM base URL / model / key** | `.env` | Restart `start mcp.bat` |
| **EMBED_DIMS** | `.env` | Restart `start mcp.bat` (auto-heals table if dims mismatch) |
| **pgvector DSN** | `.env` | Restart `start mcp.bat` + `start api.bat` |

---

## 5. Where Settings Flow in the Code

```
.env
 │
 ├─ docker-compose.yml ──────────────────────────── Docker services
 │     ├─ splunk        (SPLUNK_PASSWORD, SPLUNK_HEC_TOKEN)
 │     ├─ log-generator (SPLUNK_HEC_URL, SPLUNK_HEC_TOKEN, SPLUNK_INDEX)
 │     └─ demo-rca-app  (SPLUNK_HEC_URL, SPLUNK_HEC_TOKEN, SPLUNK_INDEX)
 │
 ├─ start mcp.bat ──────────────────────────────── Local: MCP Server :8001
 │     Overrides: SPLUNK_*=localhost, LLM_BASE_URL=localhost:1234
 │     Reads at startup → mcp_server/splunk_client.py
 │                      → mcp_server/llm_client.py
 │                      → mcp_server/pgvector_client.py
 │
 └─ start api.bat ──────────────────────────────── Local: API Server :8000
       Overrides: MCP_SERVER_URL=localhost:8001, FRONTEND_DIR=./frontend
       Does NOT read Splunk/LLM directly — proxies through MCP server
```

---

## 6. Starting the Platform

### Prerequisites
- Docker Desktop running
- LM Studio running with chat + embedding models loaded

### Step 1 — Start Docker infrastructure
```bat
docker compose up -d
```
Starts: Splunk, pgvector, log-generator, demo apps.
Wait ~90 seconds for Splunk to initialise before running RCA.

### Step 2 — Start MCP server (Terminal 1)
```bat
start mcp.bat
```
Wait for: `Application startup complete.`

### Step 3 — Start API server (Terminal 2)
```bat
start api.bat
```
Wait for: `Application startup complete.`

### Step 4 — Open the UI
```
http://localhost:8000
```

### Verify everything is working
```bat
run tests.bat
```
All 9 tests should pass.

---

## Quick Reference — All Settings

```ini
# ── Splunk ─────────────────────────────────────────────────
SPLUNK_HEC_TOKEN=rca-hec-token-00000000-0000-0000-0000-000000000001
SPLUNK_PASSWORD=changeme
SPLUNK_HEC_URL=http://localhost:8088/services/collector/event
SPLUNK_REST_URL=https://localhost:8089
SPLUNK_INDEX=rca_logs

# ── LLM (LM Studio) ───────────────────────────────────────
LLM_BASE_URL=http://host.docker.internal:1234/v1
LLM_API_KEY=lm-studio
LLM_CHAT_MODEL=meta-llama-3.1-8b-instruct@q5_k_m
EMBED_MODEL=text-embedding-nomic-embed-text-v1.5
EMBED_DIMS=768

# ── pgvector ───────────────────────────────────────────────
PGVECTOR_DSN=postgresql://rca:rca@localhost:5432/rca_db

# ── Log generator ──────────────────────────────────────────
TARGET_MB_PER_HOUR=1024
WORKER_THREADS=4
APP_IDS=app-alpha,app-beta,app-gamma,app-delta,app-epsilon
```
