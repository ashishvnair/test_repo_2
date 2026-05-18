"""
pgvector_proxy.py — Async wrappers around pgvector_client (sync) for api_server.

The api_server is async (FastAPI + uvicorn). The pgvector_client uses psycopg2
(synchronous). asyncio.to_thread() runs each sync DB call in a thread pool,
keeping the event loop unblocked.
"""

import asyncio
import os
import sys

# pgvector_client lives in mcp_server/ package.
# When api_server and mcp_server share the same PYTHONPATH (/app),
# we import it directly. In testing, fall back gracefully.
sys.path.insert(0, "/app")

try:
    from mcp_server import pgvector_client as pgdb
except ImportError:
    pgdb = None  # type: ignore


async def list_apps() -> list:
    if not pgdb:
        return []
    return await asyncio.to_thread(pgdb.list_apps)


async def get_app(app_id: str):
    if not pgdb:
        return None
    return await asyncio.to_thread(pgdb.get_app, app_id)


async def upsert_app(app: dict) -> dict:
    if not pgdb:
        return {}
    return await asyncio.to_thread(pgdb.upsert_app, app)


async def delete_app(app_id: str) -> bool:
    if not pgdb:
        return False
    return await asyncio.to_thread(pgdb.delete_app, app_id)


async def stats() -> dict:
    if not pgdb:
        return {"total": 0}
    total = await asyncio.to_thread(pgdb.count_all)
    return {"total_reports": total}


async def counts_by_category(app_id: str = "default") -> dict:
    if not pgdb:
        return {}
    return await asyncio.to_thread(pgdb.counts_by_category, app_id)


async def get_by_category(category: str, app_id: str = "default", limit: int = 20) -> list:
    if not pgdb:
        return []
    return await asyncio.to_thread(pgdb.get_by_category, category, app_id, limit)


async def reset(app_id=None) -> dict:
    if not pgdb:
        return {"deleted": 0}
    deleted = await asyncio.to_thread(pgdb.reset, app_id)
    return {"deleted": deleted}
