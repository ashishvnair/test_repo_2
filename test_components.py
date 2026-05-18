"""
test_components.py — RCA Platform Component Tester

Tests every integration point and prints a colour-coded pass/fail report.
Run from the project root:

    python test_components.py

Or via the launcher batch file:

    run_tests.bat

Components tested
-----------------
  1  LLM Chat          — POST /v1/chat/completions → raw response
  2  LLM Embedding     — POST /v1/embeddings → vector dimensions
  3  Splunk REST API   — GET  /services/server/info (auth check)
  4  Splunk Search     — POST /services/search/jobs → latest events from SPLUNK_INDEX
  5  HEC Ingest        — POST /services/collector/event (write test event)
  6  pgvector DB       — SELECT count(*) FROM rca_reports
  7  MCP Server        — GET  /health
  8  API Server        — GET  /api/health
  9  Vector Store      — MCP tool: generate_embedding + store/search round-trip
"""

import json
import os
import sys
import time
import traceback
import urllib.request
import urllib.error
import urllib.parse
import ssl
import base64

# ─────────────────────────────────────────────────────────────────────────────
# Config (read from .env if present, else fall back to defaults)
# ─────────────────────────────────────────────────────────────────────────────

def _load_env():
    env = {}
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    return env

_env = _load_env()
def _get(key, default=""):
    return os.environ.get(key) or _env.get(key) or default

LLM_BASE_URL    = _get("LLM_BASE_URL",   "http://localhost:1234/v1")
LLM_API_KEY     = _get("LLM_API_KEY",    "lm-studio")
LLM_CHAT_MODEL  = _get("LLM_CHAT_MODEL", "local-model")
EMBED_MODEL     = _get("EMBED_MODEL",    "text-embedding-nomic-embed-text-v1.5")
EMBED_DIMS      = int(_get("EMBED_DIMS", "768"))

SPLUNK_REST_URL = _get("SPLUNK_REST_URL", "https://localhost:8089")
SPLUNK_HEC_URL  = _get("SPLUNK_HEC_URL",  "http://localhost:8088/services/collector/event")
SPLUNK_PASSWORD = _get("SPLUNK_PASSWORD", "changeme")
SPLUNK_INDEX    = _get("SPLUNK_INDEX",    "rca_logs")
SPLUNK_USER     = "admin"

PGVECTOR_DSN    = _get("PGVECTOR_DSN", "postgresql://rca:rca@localhost:5432/rca_db")

MCP_URL         = "http://localhost:8001"
API_URL         = "http://localhost:8000"

# ─────────────────────────────────────────────────────────────────────────────
# Colour helpers (Windows ANSI — works in Windows Terminal / PowerShell 7)
# ─────────────────────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode    = ssl.CERT_NONE

def _http(method, url, body=None, headers=None, timeout=15):
    """Minimal HTTP helper — returns (status_code, response_body_str)."""
    data    = json.dumps(body).encode() if body is not None else None
    hdrs    = {"Content-Type": "application/json", **(headers or {})}
    req     = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    ctx     = _ssl_ctx if url.startswith("https") else None
    resp    = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    return resp.status, resp.read().decode()

def _http_basic(url, user, password, timeout=15):
    creds = base64.b64encode(f"{user}:{password}".encode()).decode()
    return _http("GET", url, headers={"Authorization": f"Basic {creds}"}, timeout=timeout)

def _post_form(url, data_str, user, password, timeout=20):
    """POST application/x-www-form-urlencoded with basic auth."""
    data  = data_str.encode()
    creds = base64.b64encode(f"{user}:{password}".encode()).decode()
    req   = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type":  "application/x-www-form-urlencoded",
            "Authorization": f"Basic {creds}",
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx)
    return resp.status, resp.read().decode()

# ─────────────────────────────────────────────────────────────────────────────
# Result tracking
# ─────────────────────────────────────────────────────────────────────────────

_results = []   # [(name, passed, detail)]

def _pass(name, detail=""):
    _results.append((name, True, detail))
    tick = f"{GREEN}✓ PASS{RESET}"
    print(f"  {tick}  {BOLD}{name}{RESET}  {CYAN}{detail}{RESET}")

def _fail(name, detail=""):
    _results.append((name, False, detail))
    cross = f"{RED}✗ FAIL{RESET}"
    print(f"  {cross}  {BOLD}{name}{RESET}  {YELLOW}{detail}{RESET}")

def _section(title):
    print(f"\n{BOLD}{CYAN}{'─'*60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'─'*60}{RESET}")

# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — LLM Chat
# ─────────────────────────────────────────────────────────────────────────────

def test_llm_chat():
    _section("1 · LLM Chat  (LM Studio)")
    print(f"  URL   : {LLM_BASE_URL}")
    print(f"  Model : {LLM_CHAT_MODEL}")
    try:
        status, body = _http("POST", f"{LLM_BASE_URL}/chat/completions",
            body={
                "model":      LLM_CHAT_MODEL,
                "messages":   [{"role": "user", "content": "Reply with: OK"}],
                "max_tokens": 10,
                "temperature": 0,
            },
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            timeout=30,
        )
        data    = json.loads(body)
        content = data["choices"][0]["message"]["content"].strip()
        _pass("LLM Chat", f"HTTP {status} | response: '{content[:80]}'")
    except urllib.error.URLError as e:
        _fail("LLM Chat", f"Cannot reach {LLM_BASE_URL} — is LM Studio running? ({e.reason})")
    except Exception as e:
        _fail("LLM Chat", f"{type(e).__name__}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — LLM Embedding
# ─────────────────────────────────────────────────────────────────────────────

def test_llm_embedding():
    _section("2 · LLM Embedding  (LM Studio)")
    print(f"  URL   : {LLM_BASE_URL}")
    print(f"  Model : {EMBED_MODEL}  (expected {EMBED_DIMS} dims)")
    try:
        status, body = _http("POST", f"{LLM_BASE_URL}/embeddings",
            body={"model": EMBED_MODEL, "input": "test connectivity"},
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            timeout=30,
        )
        data   = json.loads(body)
        vector = data["data"][0]["embedding"]
        dims   = len(vector)
        if dims == EMBED_DIMS:
            _pass("LLM Embedding", f"HTTP {status} | {dims} dims ✓")
        else:
            _fail("LLM Embedding",
                  f"Got {dims} dims but EMBED_DIMS={EMBED_DIMS} — "
                  f"update EMBED_DIMS in .env to {dims}")
    except urllib.error.URLError as e:
        _fail("LLM Embedding", f"Cannot reach {LLM_BASE_URL} — is embed model loaded? ({e.reason})")
    except Exception as e:
        _fail("LLM Embedding", f"{type(e).__name__}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — Splunk REST auth
# ─────────────────────────────────────────────────────────────────────────────

def test_splunk_auth():
    _section("3 · Splunk REST API  (auth)")
    print(f"  URL  : {SPLUNK_REST_URL}")
    print(f"  User : {SPLUNK_USER} / ****")
    try:
        status, body = _http_basic(
            f"{SPLUNK_REST_URL}/services/server/info?output_mode=json",
            SPLUNK_USER, SPLUNK_PASSWORD, timeout=15,
        )
        data    = json.loads(body)
        version = data.get("entry", [{}])[0].get("content", {}).get("version", "?")
        _pass("Splunk REST Auth", f"HTTP {status} | Splunk {version}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _fail("Splunk REST Auth", f"401 Unauthorized — check SPLUNK_PASSWORD in .env")
        else:
            _fail("Splunk REST Auth", f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        _fail("Splunk REST Auth", f"Cannot reach {SPLUNK_REST_URL} — is Splunk running? ({e.reason})")
    except Exception as e:
        _fail("Splunk REST Auth", f"{type(e).__name__}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — Splunk search (pull latest events)
# ─────────────────────────────────────────────────────────────────────────────

def test_splunk_search():
    _section("4 · Splunk Search  (latest events)")
    print(f"  Index : {SPLUNK_INDEX}")
    try:
        # Submit search job
        search_spl = f"search index={SPLUNK_INDEX} | head 5 | fields _time, _raw"
        status, body = _post_form(
            f"{SPLUNK_REST_URL}/services/search/jobs?output_mode=json",
            f"search={urllib.parse.quote(search_spl)}&earliest_time=-15m&latest_time=now",
            SPLUNK_USER, SPLUNK_PASSWORD, timeout=20,
        )
        sid = json.loads(body)["sid"]

        # Poll until done (max 20s)
        for _ in range(20):
            time.sleep(1)
            _, rbody = _http_basic(
                f"{SPLUNK_REST_URL}/services/search/jobs/{sid}?output_mode=json",
                SPLUNK_USER, SPLUNK_PASSWORD, timeout=10,
            )
            state = json.loads(rbody)["entry"][0]["content"]["dispatchState"]
            if state in ("DONE", "FAILED"):
                break

        if state == "FAILED":
            _fail("Splunk Search", "Search job FAILED — check SPL and index name")
            return

        # Fetch results
        _, rbody = _http_basic(
            f"{SPLUNK_REST_URL}/services/search/jobs/{sid}/results?output_mode=json&count=5",
            SPLUNK_USER, SPLUNK_PASSWORD, timeout=10,
        )
        results = json.loads(rbody).get("results", [])
        if results:
            sample = results[0].get("_raw", "")[:80]
            _pass("Splunk Search", f"{len(results)} events found | sample: '{sample}…'")
        else:
            _fail("Splunk Search",
                  f"Index '{SPLUNK_INDEX}' returned 0 events in last 15 min — "
                  f"is the log generator running? (docker ps | grep log-generator)")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _fail("Splunk Search", "401 — auth failed")
        else:
            _fail("Splunk Search", f"HTTP {e.code}")
    except Exception as e:
        _fail("Splunk Search", f"{type(e).__name__}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — Splunk HEC ingest
# ─────────────────────────────────────────────────────────────────────────────

def test_splunk_hec():
    _section("5 · Splunk HEC Ingest  (write test)")
    print(f"  URL   : {SPLUNK_HEC_URL}")
    print(f"  Index : {SPLUNK_INDEX}")
    try:
        status, body = _http("POST", SPLUNK_HEC_URL,
            body={"event": "RCA_TESTER: connectivity check", "index": SPLUNK_INDEX,
                  "sourcetype": "rca_tester"},
            headers={"Authorization": f"Splunk {_get('SPLUNK_HEC_TOKEN', 'rca-hec-token-00000000-0000-0000-0000-000000000001')}"},
            timeout=10,
        )
        data = json.loads(body)
        if data.get("text") == "Success":
            _pass("Splunk HEC", f"HTTP {status} | event accepted")
        else:
            _fail("Splunk HEC", f"Unexpected response: {body[:120]}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        _fail("Splunk HEC", f"HTTP {e.code} — {body[:120]}")
    except urllib.error.URLError as e:
        _fail("Splunk HEC", f"Cannot reach HEC at {SPLUNK_HEC_URL} ({e.reason})")
    except Exception as e:
        _fail("Splunk HEC", f"{type(e).__name__}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Test 6 — pgvector database
# ─────────────────────────────────────────────────────────────────────────────

def test_pgvector():
    _section("6 · pgvector Database")
    print(f"  DSN : {PGVECTOR_DSN}")
    try:
        import psycopg2
        conn = psycopg2.connect(PGVECTOR_DSN)
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM rca_reports;")
        count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM apps;")
        apps  = cur.fetchone()[0]
        cur.execute("SELECT atttypmod FROM pg_attribute WHERE attrelid='rca_reports'::regclass AND attname='embedding';")
        dims  = cur.fetchone()[0]
        conn.close()
        _pass("pgvector DB", f"rca_reports={count} rows | apps={apps} | embedding dims={dims}")
    except ImportError:
        _fail("pgvector DB", "psycopg2 not installed — run: pip install psycopg2-binary")
    except Exception as e:
        _fail("pgvector DB", f"{type(e).__name__}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Test 7 — MCP server health
# ─────────────────────────────────────────────────────────────────────────────

def test_mcp_server():
    _section("7 · MCP Server  (tool server)")
    print(f"  URL : {MCP_URL}")
    try:
        status, body = _http("GET", f"{MCP_URL}/health", timeout=30)
        data = json.loads(body)
        splunk_ok = data.get("splunk_ok", "?")
        db_ok     = data.get("db_ok", "?")
        llm_ok    = data.get("llm_ok", "?")
        _pass("MCP Server", f"HTTP {status} | splunk={splunk_ok} db={db_ok} llm={llm_ok}")

        # Also list tools
        status2, body2 = _http("GET", f"{MCP_URL}/tools", timeout=10)
        tools = json.loads(body2).get("tools", [])
        _pass("MCP Tools", f"{len(tools)} tools registered: {', '.join(tools)}")
    except urllib.error.URLError as e:
        _fail("MCP Server", f"Cannot reach {MCP_URL} — is 'start mcp.bat' running? ({e.reason})")
    except Exception as e:
        _fail("MCP Server", f"{type(e).__name__}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Test 8 — API server health
# ─────────────────────────────────────────────────────────────────────────────

def test_api_server():
    _section("8 · API Server  (FastAPI)")
    print(f"  URL : {API_URL}")
    try:
        status, body = _http("GET", f"{API_URL}/api/health", timeout=10)
        data = json.loads(body)
        mcp_status = data.get("mcp_status", "?")
        added      = data.get("reports_added", 0)
        _pass("API Server", f"HTTP {status} | mcp_status={mcp_status} | reports_added={added}")

        # Check registered apps
        status2, body2 = _http("GET", f"{API_URL}/api/apps", timeout=10)
        apps = json.loads(body2)
        if isinstance(apps, list):
            app_ids = [a.get("app_id","?") for a in apps]
            _pass("App Registry", f"{len(apps)} apps: {', '.join(app_ids[:8])}")
        else:
            _fail("App Registry", f"Unexpected response: {str(body2)[:80]}")
    except urllib.error.URLError as e:
        _fail("API Server", f"Cannot reach {API_URL} — is 'start api.bat' running? ({e.reason})")
    except Exception as e:
        _fail("API Server", f"{type(e).__name__}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Test 9 — End-to-end vector round-trip via MCP tools
# ─────────────────────────────────────────────────────────────────────────────

def test_vector_roundtrip():
    _section("9 · Vector Store Round-trip  (embed → search)")
    print(f"  MCP : {MCP_URL}/tools/generate_embedding")
    try:
        # Generate embedding via MCP
        status, body = _http("POST", f"{MCP_URL}/tools/generate_embedding",
            body={"text": "DB_CONN error: connection refused to database host"},
            timeout=30,
        )
        data = json.loads(body)
        emb  = data.get("embedding", [])
        if not emb:
            _fail("Vector Embed", "generate_embedding returned empty vector")
            return
        _pass("Vector Embed", f"embedding length={len(emb)}")

        # Search via MCP
        status2, body2 = _http("POST", f"{MCP_URL}/tools/search_similar_rca",
            body={
                "incident_text":       "DB_CONN error",
                "embedding":           emb,
                "app_id":              "app-alpha",
                "incident_category":   "database-connection",
                "incident_error_type": "DB_CONN",
                "incident_pill_text":  "DB_CONN connection refused",
            },
            timeout=30,
        )
        data2     = json.loads(body2)
        hits      = data2.get("hits", [])
        n_similar = len(data2.get("similar_reports", []))
        _pass("Vector Search", f"{len(hits)} hits | {n_similar} above similarity threshold")
    except urllib.error.URLError as e:
        _fail("Vector Round-trip", f"Cannot reach MCP server ({e.reason})")
    except Exception as e:
        _fail("Vector Round-trip", f"{type(e).__name__}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

def print_summary():
    passed = [r for r in _results if r[1]]
    failed = [r for r in _results if not r[1]]

    print(f"\n{BOLD}{CYAN}{'═'*60}{RESET}")
    print(f"{BOLD}  RESULTS  {len(passed)}/{len(_results)} passed{RESET}")
    print(f"{BOLD}{CYAN}{'═'*60}{RESET}")

    if failed:
        print(f"\n{RED}{BOLD}  FAILED:{RESET}")
        for name, _, detail in failed:
            print(f"    {RED}✗{RESET}  {BOLD}{name}{RESET}")
            if detail:
                print(f"       → {YELLOW}{detail}{RESET}")
        print()

    if not failed:
        print(f"\n{GREEN}{BOLD}  All systems operational.{RESET}\n")
    else:
        print(f"{YELLOW}  Fix the failed components above, then re-run this script.{RESET}\n")

    return len(failed)

# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n{BOLD}{CYAN}{'═'*60}{RESET}")
    print(f"{BOLD}{CYAN}  RCA Platform — Component Tests{RESET}")
    print(f"{BOLD}{CYAN}{'═'*60}{RESET}")
    print(f"  Config loaded from: .env")
    print(f"  LLM      : {LLM_BASE_URL}")
    print(f"  Splunk   : {SPLUNK_REST_URL}")
    print(f"  pgvector : {PGVECTOR_DSN}")
    print(f"  MCP      : {MCP_URL}")
    print(f"  API      : {API_URL}")

    test_llm_chat()
    test_llm_embedding()
    test_splunk_auth()
    test_splunk_search()
    test_splunk_hec()
    test_pgvector()
    test_mcp_server()
    test_api_server()
    test_vector_roundtrip()

    failed = print_summary()
    sys.exit(1 if failed else 0)
