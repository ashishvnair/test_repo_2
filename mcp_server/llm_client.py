"""
llm_client.py — LLM client supporting both LM Studio (local) and
Bearer-token corporate endpoints (OpenAI-compatible).

Auth mode is selected by env vars:
  - If LLM_AUTH_TOKEN is set  → Bearer token mode (corporate/hackathon endpoint)
  - Otherwise                 → LLM_API_KEY mode (LM Studio / OpenAI cloud)

SSL:
  - If LLM_SSL_CERT_FILE is set → that PEM file is set as SSL_CERT_FILE

Token refresh:
  - Bearer tokens typically expire every ~20 min.
  - Call set_auth_token(new_token) or POST /update-token on the MCP server
    to hot-swap the token without restarting the server.
"""

import json
import logging
import os
import re
import threading
from typing import Any, Optional

from openai import OpenAI

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration from environment
# ─────────────────────────────────────────────────────────────────────────────
LLM_BASE_URL   = os.getenv("LLM_BASE_URL",    "http://host.docker.internal:1234/v1")
LLM_API_KEY    = os.getenv("LLM_API_KEY",     "lm-studio")
LLM_SSL_CERT   = os.getenv("LLM_SSL_CERT_FILE", "")
CHAT_MODEL     = os.getenv("LLM_CHAT_MODEL",  "meta-llama-3.1-8b-instruct@q3_k_l")
EMBED_MODEL    = os.getenv("EMBED_MODEL",     "text-embedding-nomic-embed-text-v1.5")
EMBED_DIMS     = int(os.getenv("EMBED_DIMS",  "1024"))

# Apply SSL cert override at import time so all http libs pick it up
if LLM_SSL_CERT:
    os.environ["SSL_CERT_FILE"]      = LLM_SSL_CERT
    os.environ["REQUESTS_CA_BUNDLE"] = LLM_SSL_CERT
    logger.info("SSL_CERT_FILE set to: %s", LLM_SSL_CERT)

# ─────────────────────────────────────────────────────────────────────────────
# Mutable token state — updated by set_auth_token() without server restart
# ─────────────────────────────────────────────────────────────────────────────
_token_lock            = threading.Lock()
_current_auth_token    = os.getenv("LLM_AUTH_TOKEN", "")  # live token
_client: Optional[OpenAI] = None
_working_chat_model: Optional[str] = None


def set_auth_token(token: str) -> None:
    """
    Hot-swap the Bearer token without restarting the MCP server.

    The next LLM call after this returns will use the new token.
    Called by the POST /update-token endpoint on the MCP server.
    """
    global _client, _current_auth_token, _working_chat_model
    with _token_lock:
        _current_auth_token = token.strip()
        _client = None              # force rebuild with new token on next call
        _working_chat_model = None  # re-probe model list with new token
    logger.info("LLM auth token updated (len=%d); client will reconnect on next call", len(token))


def _get_client() -> OpenAI:
    """
    Return (or lazily build) the OpenAI client.

    Always reads _current_auth_token so a set_auth_token() call
    is picked up on the very next request.
    """
    global _client
    with _token_lock:
        if _client is None:
            if _current_auth_token:
                os.environ["OPENAI_API_KEY"] = "DUMMY"
                _client = OpenAI(
                    base_url=LLM_BASE_URL,
                    api_key="DUMMY",
                    default_headers={"Authorization": f"Bearer {_current_auth_token}"},
                )
                logger.info("LLM client (re)built: Bearer-token mode → %s", LLM_BASE_URL)
            else:
                _client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
                logger.info("LLM client (re)built: API-key mode → %s", LLM_BASE_URL)
    return _client


def _get_chat_model() -> str:
    """
    Return a model ID that actually responds to chat completions.

    Tries the configured CHAT_MODEL first, then probes /v1/models if needed.
    """
    global _working_chat_model
    if _working_chat_model:
        return _working_chat_model

    import httpx

    candidates = [CHAT_MODEL]
    try:
        headers = {}
        with _token_lock:
            tok = _current_auth_token
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        verify = LLM_SSL_CERT if LLM_SSL_CERT else True
        resp = httpx.get(
            f"{LLM_BASE_URL}/models",
            headers=headers,
            verify=verify,
            timeout=5.0,
        )
        if resp.status_code == 200:
            for m in resp.json().get("data", []):
                mid = m.get("id", "")
                if "embed" not in mid.lower() and mid not in candidates:
                    candidates.append(mid)
    except Exception as exc:
        logger.warning("Could not fetch model list: %s", exc)

    for mid in candidates:
        try:
            resp = _get_client().chat.completions.create(
                model=mid,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=3,
                temperature=0.0,
                user=os.getenv("USERNAME", ""),
            )
            if resp.choices and resp.choices[0].message.content is not None:
                logger.info("Auto-detected working chat model: %s", mid)
                _working_chat_model = mid
                return mid
        except Exception:
            continue

    logger.warning("No working chat model found — using %s (may fail)", CHAT_MODEL)
    _working_chat_model = CHAT_MODEL
    return CHAT_MODEL


def embed(text: str, model: str = "") -> list[float]:
    """Embed text. Returns a list of EMBED_DIMS floats."""
    model = model or EMBED_MODEL
    text = text[:8000]
    response = _get_client().embeddings.create(model=model, input=[text])
    return response.data[0].embedding


def _extract_json(text: str) -> Optional[dict]:
    """Extract a JSON object from arbitrary LLM output text."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find('{')
    end   = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return None


def call_llm(
    prompt: str,
    system: str,
    max_tokens: int = 1500,
    temperature: float = 0.3,
    max_retries: int = 5,
    required_keys: Optional[list[str]] = None,
) -> dict:
    """
    Call LLM chat completions with a self-correcting retry loop.

    Returns dict: {content, parsed, attempts, _failed}
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": prompt},
    ]
    last_content = ""
    model = _get_chat_model()

    for attempt in range(1, max_retries + 1):
        try:
            response = _get_client().chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                user=os.getenv("USERNAME", ""),
            )
            content = response.choices[0].message.content or ""
            last_content = content

            logger.debug("LLM raw output (attempt %d): %s", attempt, content[:500])
            parsed = _extract_json(content)
            if parsed is None:
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        "Your response could not be parsed as JSON. "
                        "Please respond with ONLY a valid JSON object, no prose, "
                        "no markdown fences. Try again."
                    ),
                })
                logger.warning("LLM attempt %d/%d: JSON parse failed", attempt, max_retries)
                continue

            logger.info("LLM succeeded on attempt %d/%d", attempt, max_retries)
            return {"content": content, "parsed": parsed, "attempts": attempt, "_failed": False}

        except Exception as exc:
            logger.error("LLM attempt %d/%d exception: %s", attempt, max_retries, exc)
            if attempt == max_retries:
                break
            continue

    logger.error("LLM failed after %d attempts", max_retries)
    return {"content": last_content, "parsed": None, "attempts": max_retries, "_failed": True}


def call_llm_reasoning(prompt: str, system: str, max_tokens: int = 800) -> str:
    """Pass 1 of scratchpad pattern: free-text reasoning, no JSON required."""
    try:
        response = _get_client().chat.completions.create(
            model=_get_chat_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.5,
            user=os.getenv("USERNAME", ""),
        )
        return response.choices[0].message.content or ""
    except Exception as exc:
        logger.error("LLM reasoning call failed: %s", exc)
        return f"[reasoning failed: {exc}]"


def health_check() -> bool:
    """Return True if LLM endpoint is reachable and embeddings respond."""
    try:
        result = embed("health check")
        return len(result) == EMBED_DIMS
    except Exception:
        return False
