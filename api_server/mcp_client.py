"""
mcp_client.py — Plain HTTP client for the MCP tool server.

The mcp-server exposes every RCA tool as a plain HTTP endpoint:
  POST /tools/{tool_name}   — call a tool with JSON arguments
  GET  /tools               — list available tools
  GET  /health              — health check

We use plain httpx here (no MCP wire protocol / StreamableHTTP) because
mcp_server/server.py was written as a standard FastAPI app rather than using
the FastMCP library. Plain HTTP is simpler to debug, curl-able, and has no
version dependency on the MCP Python SDK.

Architecture:
  Browser → api-server:8000 → mcp-server:8001/tools/* → Splunk/pgvector/LLM

All RCA logic lives in mcp-server. api-server is a thin SSE orchestrator that
calls these tools in sequence and streams progress back to the browser.
"""

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://mcp-server:8001")


async def call_tool(tool_name: str, arguments: dict) -> Any:
    """
    Call a single tool on the mcp-server and return its result.

    Opens a fresh httpx session, POSTs the arguments as JSON to
    /tools/{tool_name}, and returns the parsed JSON response body.

    Parameters
    ----------
    tool_name  : Name of the tool  (e.g. "fetch_logs_splunk")
    arguments  : Tool input arguments as a plain dict

    Returns
    -------
    The tool's result — always a dict (the tool implementations all return dicts).

    Raises
    ------
    httpx.HTTPStatusError  if the server returns 4xx/5xx
    httpx.ConnectError     if mcp-server is unreachable
    """
    url = f"{MCP_SERVER_URL}/tools/{tool_name}"
    logger.debug("MCP call: %s  args_keys=%s", tool_name, list(arguments.keys()))
    # Per-tool timeouts:
    #   360s — LLM calls and Splunk scans (slow by nature)
    #   30s  — fast DB/embedding tools
    #   60s  — everything else (store, search, scratchpad)
    _slow_tools  = {"call_llm", "fetch_logs_splunk", "fetch_logs_loki", "store_rca_report"}
    _fast_tools  = {"generate_embedding", "scratchpad_write", "scratchpad_read"}
    _timeout = 360.0 if tool_name in _slow_tools else (30.0 if tool_name in _fast_tools else 60.0)
    async with httpx.AsyncClient(timeout=_timeout) as client:
        resp = await client.post(url, json=arguments)
        resp.raise_for_status()
        return resp.json()


async def list_tools() -> list[str]:
    """Return the list of tool names registered on the mcp-server."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{MCP_SERVER_URL}/tools")
        resp.raise_for_status()
        return resp.json().get("tools", [])


async def health() -> bool:
    """Return True if the mcp-server /health endpoint responds 200."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{MCP_SERVER_URL}/health")
            return resp.status_code == 200
    except Exception as exc:
        logger.debug("MCP health check failed: %s", exc)
        return False
