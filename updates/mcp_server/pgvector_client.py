"""
pgvector_client.py — PostgreSQL + pgvector database client.

Replaces rca_2/backend/database.py (ChromaDB).

Tables
------
  apps        — app registry (previously apps_registry.json)
  rca_reports — accepted RCA reports with VECTOR(1024) embeddings

Cosine Similarity
-----------------
  pgvector uses the <=> operator for cosine distance (0 = identical, 2 = opposite).
  This is the same scale as ChromaDB's cosine distance, so the
  KNOWN_ISSUE_DISTANCE_THRESHOLD=0.3 value carries over unchanged.

  pgvector HNSW index is equivalent to ChromaDB's HNSW with hnsw:space=cosine.

Connection Pooling
------------------
  psycopg2 is used (synchronous, compatible with ThreadPoolExecutor batch processor).
  A threading.local() pool-per-thread pattern ensures each thread has its own
  connection without lock contention. Connections are created lazily.

Why psycopg2 instead of asyncpg
--------------------------------
  The batch_processor uses ThreadPoolExecutor, not asyncio. asyncpg requires
  an event loop per connection. psycopg2 is synchronous and works naturally
  with threads. The MCP server tools that are called from async context
  (FastMCP) run the sync DB calls in a thread pool via asyncio.to_thread().
"""

import json
import logging
import os
import threading
import uuid
from typing import Any, Optional
from dataclasses import dataclass

import psycopg2
import psycopg2.extras
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

PGVECTOR_DSN = os.getenv("PGVECTOR_DSN", "postgresql://rca:rca@pgvector:5432/rca_db")
EMBED_DIMS = int(os.getenv("EMBED_DIMS", "1024"))
KNOWN_ISSUE_THRESHOLD = float(os.getenv("KNOWN_ISSUE_DISTANCE_THRESHOLD", "0.3"))

# Register pgvector's numpy/list type adapter so Python lists are treated as vectors
try:
    from pgvector.psycopg2 import register_vector
    _pgvector_registered = True
except ImportError:
    _pgvector_registered = False
    logger.warning("pgvector Python package not found; vector type may need manual casting")

# Thread-local connection storage — each thread gets its own psycopg2 connection
_thread_local = threading.local()


def _try_register_vector(conn):
    """Register the pgvector type adapter if the extension is installed."""
    if not _pgvector_registered:
        return
    try:
        register_vector(conn)
    except Exception:
        pass  # extension not yet created; will be registered after ensure_schema()


def _get_conn():
    """Return a thread-local psycopg2 connection, creating/resetting if needed."""
    conn = getattr(_thread_local, "conn", None)
    if conn is None or conn.closed:
        conn = psycopg2.connect(PGVECTOR_DSN)
        conn.autocommit = False
        _try_register_vector(conn)
        _thread_local.conn = conn
    else:
        # If the connection is stuck in a failed/aborted transaction, roll it back
        if conn.status == psycopg2.extensions.STATUS_IN_TRANSACTION:
            try:
                conn.rollback()
            except Exception:
                # Connection is broken — make a fresh one
                try:
                    conn.close()
                except Exception:
                    pass
                conn = psycopg2.connect(PGVECTOR_DSN)
                conn.autocommit = False
                _try_register_vector(conn)
                _thread_local.conn = conn

        # Liveness ping — detect server-closed connections (e.g. pgvector
        # container restarted, idle timeout, TCP keepalive failure).
        try:
            with conn.cursor() as _cur:
                _cur.execute("SELECT 1")
            conn.rollback()  # keep connection clean after ping
        except Exception:
            logger.warning("pgvector connection lost; reconnecting")
            try:
                conn.close()
            except Exception:
                pass
            conn = psycopg2.connect(PGVECTOR_DSN)
            conn.autocommit = False
            _try_register_vector(conn)
            _thread_local.conn = conn

    return conn


@dataclass
class SimilarityHit:
    """A single result from a vector similarity search."""
    id: str
    app_id: str
    error_type: str
    category: str
    report: dict                # full RCAReport as dict
    cosine_distance: float      # 0=identical, 2=opposite
    created_at: str


# ─────────────────────────────────────────────────────────────────────────────
# Schema management
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA_SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS apps (
    app_id          TEXT PRIMARY KEY,
    app_name        TEXT NOT NULL,
    service_name    TEXT NOT NULL,
    port            INT,
    container_name  TEXT,
    vector_category TEXT NOT NULL DEFAULT 'default',
    source_config   JSONB NOT NULL DEFAULT '{{}}',
    enabled_sources TEXT[] NOT NULL DEFAULT '{{splunk}}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rca_reports (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_id          TEXT NOT NULL DEFAULT 'default',
    error_type      TEXT NOT NULL DEFAULT 'UNKNOWN',
    category        TEXT NOT NULL DEFAULT 'unknown',
    report          JSONB NOT NULL,
    embed_source    TEXT NOT NULL DEFAULT '',
    embedding       VECTOR({EMBED_DIMS}) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cluster_id      UUID
);

CREATE INDEX IF NOT EXISTS rca_embedding_hnsw_idx
    ON rca_reports USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);

CREATE INDEX IF NOT EXISTS rca_app_category_idx
    ON rca_reports (app_id, category);

CREATE INDEX IF NOT EXISTS rca_created_at_idx
    ON rca_reports (created_at DESC);
"""


def _fix_embedding_dimensions(conn) -> None:
    """
    Detect and fix a dimension mismatch on the embedding column.

    If rca_reports exists but was created with a different vector dimension
    (e.g. VECTOR(1024) when EMBED_DIMS=768), drop and recreate the table
    so the server starts cleanly instead of silently failing every store call.
    """
    with conn.cursor() as cur:
        # Check whether the table exists at all
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'rca_reports'
        """)
        if cur.fetchone()[0] == 0:
            return  # table doesn't exist yet — ensure_schema will create it

        # Read the stored vector dimension from pg_attribute
        cur.execute("""
            SELECT atttypmod
            FROM   pg_attribute
            WHERE  attrelid = 'rca_reports'::regclass
            AND    attname  = 'embedding'
        """)
        row = cur.fetchone()
        if row is None:
            return  # column not found — table will be recreated

        stored_dims = row[0]   # pgvector stores ndim in atttypmod
        if stored_dims == EMBED_DIMS:
            return  # already correct

        logger.warning(
            "Embedding dimension mismatch: table has %s dims, EMBED_DIMS=%s. "
            "Dropping rca_reports and recreating with correct dimensions.",
            stored_dims, EMBED_DIMS,
        )
        cur.execute("DROP TABLE IF EXISTS rca_reports CASCADE")
    conn.commit()


def ensure_schema() -> None:
    """
    Create the pgvector extension, tables, and indexes if they don't exist.
    Also fixes embedding dimension mismatches automatically on startup.
    Called once on MCP server startup.
    """
    conn = _get_conn()
    try:
        # First: create extension so regclass lookups work, then check dimensions
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        conn.commit()
        _try_register_vector(conn)

        # Auto-fix dimension mismatch before running the full schema SQL
        _fix_embedding_dimensions(conn)

        with conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
        conn.commit()
        _try_register_vector(conn)
        logger.info(
            "pgvector schema ensured (EMBED_DIMS=%s, extension + tables + indexes)",
            EMBED_DIMS,
        )
    except Exception as exc:
        conn.rollback()
        logger.error("Schema creation failed: %s", exc)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# App Registry (replaces apps_registry.json)
# ─────────────────────────────────────────────────────────────────────────────

def list_apps() -> list[dict]:
    """Return all apps from the registry."""
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM apps ORDER BY created_at DESC")
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        conn.rollback()
        raise


def get_app(app_id: str) -> Optional[dict]:
    """Return one app by app_id, or None if not found."""
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM apps WHERE app_id = %s", (app_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception:
        conn.rollback()
        raise


def upsert_app(app: dict) -> dict:
    """
    Insert or update an app in the registry.
    On conflict (same app_id), updates all fields except created_at.
    """
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO apps
                    (app_id, app_name, service_name, port, container_name,
                     vector_category, source_config, enabled_sources, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (app_id) DO UPDATE SET
                    app_name        = EXCLUDED.app_name,
                    service_name    = EXCLUDED.service_name,
                    port            = EXCLUDED.port,
                    container_name  = EXCLUDED.container_name,
                    vector_category = EXCLUDED.vector_category,
                    source_config   = EXCLUDED.source_config,
                    enabled_sources = EXCLUDED.enabled_sources,
                    updated_at      = NOW()
                RETURNING *
            """, (
                app["app_id"],
                app.get("app_name", app["app_id"]),
                app.get("service_name", app["app_id"]),
                app.get("port"),
                app.get("container_name"),
                app.get("vector_category", "default"),
                json.dumps(app.get("source_config", {})),
                app.get("enabled_sources", ["splunk"]),
            ))
            row = cur.fetchone()
        conn.commit()
        return dict(row)
    except Exception as exc:
        conn.rollback()
        raise


def delete_app(app_id: str) -> bool:
    """Delete an app. Returns True if a row was deleted."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM apps WHERE app_id = %s", (app_id,))
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    except Exception as exc:
        conn.rollback()
        raise


# ─────────────────────────────────────────────────────────────────────────────
# RCA Reports (replaces ChromaDB collection operations)
# ─────────────────────────────────────────────────────────────────────────────

def insert_report(
    report: dict,
    embedding: list[float],
    app_id: str = "default",
    embed_source: str = "",
    cluster_id: Optional[str] = None,
) -> str:
    """
    Insert an accepted RCA report into pgvector.

    Equivalent to ChromaDB collection.upsert().
    Returns the UUID of the inserted row.
    """
    conn = _get_conn()
    report_id = str(uuid.uuid4())
    # Support both Q&A-format reports (error_type / category at top level)
    # and legacy-format reports (nested under error_details / metadata).
    error_type = (
        report.get("error_type")                                         # Q&A: single string
        or (report.get("error_types") or ["UNKNOWN"])[0]                # Q&A: list
        or report.get("error_details", {}).get("error_code", "UNKNOWN") # legacy
        or "UNKNOWN"
    )
    category = (
        report.get("category")                                          # Q&A: top-level field
        or report.get("metadata", {}).get("category", "unknown")       # legacy
        or "unknown"
    )

    # Format embedding as pgvector literal: '[0.1,0.2,...]'
    vec_literal = "[" + ",".join(str(x) for x in embedding) + "]"

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO rca_reports
                    (id, app_id, error_type, category, report, embed_source,
                     embedding, cluster_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s)
            """, (
                report_id,
                app_id,
                error_type,
                category,
                json.dumps(report),
                embed_source,
                vec_literal,
                cluster_id,
            ))
        conn.commit()
        logger.info("Stored RCA report %s for app=%s category=%s", report_id, app_id, category)
        return report_id
    except Exception as exc:
        conn.rollback()
        logger.error("insert_report failed: %s", exc)
        raise


def query_similar(
    embedding: list[float],
    app_id: str = "default",
    n_results: int = 10,
    distance_threshold: float = KNOWN_ISSUE_THRESHOLD,
) -> list[SimilarityHit]:
    """
    Find the N most similar RCA reports using pgvector cosine distance.

    Equivalent to ChromaDB collection.query(query_embeddings=[emb], n_results=N).
    The <=> operator returns cosine distance (0=identical, 2=opposite).
    Only hits within distance_threshold are returned (mirrors ChromaDB filter).
    """
    conn = _get_conn()
    vec_literal = "[" + ",".join(str(x) for x in embedding) + "]"

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT
                id::text,
                app_id,
                error_type,
                category,
                report,
                embed_source,
                (embedding <=> %s::vector) AS cosine_distance,
                created_at::text
            FROM rca_reports
            WHERE app_id = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, (vec_literal, app_id, vec_literal, n_results))
        rows = cur.fetchall()

    # Always rollback after a read-only query so the connection is not left in
    # STATUS_IN_TRANSACTION, which would trigger an unnecessary rollback on the
    # next _get_conn() call and could confuse connection state tracking.
    try:
        conn.rollback()
    except Exception:
        pass

    logger.debug("query_similar: app_id=%s distance_threshold=%.2f returned %d rows",
                 app_id, distance_threshold, len(rows))

    hits = []
    for row in rows:
        dist = float(row["cosine_distance"])
        if dist <= distance_threshold * 2:  # scale: keep threshold consistent
            hits.append(SimilarityHit(
                id=row["id"],
                app_id=row["app_id"],
                error_type=row["error_type"],
                category=row["category"],
                report=row["report"] if isinstance(row["report"], dict) else json.loads(row["report"]),
                cosine_distance=dist,
                created_at=row["created_at"],
            ))
    return hits


def get_by_category(category: str, app_id: str = "default", limit: int = 20) -> list[dict]:
    """Return reports in a given category, newest first."""
    conn = _get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT id::text, app_id, error_type, category, report, created_at::text
            FROM rca_reports
            WHERE app_id = %s AND category = %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (app_id, category, limit))
        rows = cur.fetchall()

    return [{
        "id": r["id"],
        "app_id": r["app_id"],
        "category": r["category"],
        "report": r["report"] if isinstance(r["report"], dict) else json.loads(r["report"]),
        "created_at": r["created_at"],
    } for r in rows]


def counts_by_category(app_id: str = "default") -> dict[str, int]:
    """Return {category: count} for all categories in this app's scope."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT category, COUNT(*) AS cnt
                FROM rca_reports
                WHERE app_id = %s
                GROUP BY category
                ORDER BY cnt DESC
            """, (app_id,))
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception:
        conn.rollback()
        raise


def count_all(app_id: Optional[str] = None) -> int:
    """Total report count, optionally scoped to one app."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            if app_id:
                cur.execute("SELECT COUNT(*) FROM rca_reports WHERE app_id = %s", (app_id,))
            else:
                cur.execute("SELECT COUNT(*) FROM rca_reports")
            return cur.fetchone()[0]
    except Exception:
        conn.rollback()
        raise


def reset(app_id: Optional[str] = None) -> int:
    """Delete all reports (or all for one app). Returns deleted count."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            if app_id:
                cur.execute("DELETE FROM rca_reports WHERE app_id = %s", (app_id,))
            else:
                cur.execute("DELETE FROM rca_reports")
            deleted = cur.rowcount
        conn.commit()
        return deleted
    except Exception as exc:
        conn.rollback()
        raise


def health() -> bool:
    """Return True if the database is reachable and the tables exist."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM rca_reports")
        return True
    except Exception:
        return False
