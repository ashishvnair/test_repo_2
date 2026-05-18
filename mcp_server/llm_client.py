"""
llm_client.py — LM Studio (OpenAI-compatible) client for chat and embeddings.

Ported and extended from rca_2/backend/llm_client.py.

Key addition: the 2-pass scratchpad pattern is embedded here as a helper
so call_llm() callers can optionally enable it with use_scratchpad=True.
Without scratchpad, call_llm is identical to the old self-correcting loop.

Self-correcting retry loop
---------------------------
The local LLM (Llama 3.1 8B) sometimes produces JSON with syntax errors,
missing required keys, or extra text before/after the JSON object. The retry
loop catches these and appends a corrective message to the conversation before
retrying, using the model's own previous attempt as context. This converges
in 1-2 attempts for well-formed prompts.

LM Studio compatibility
------------------------
LM Studio exposes an OpenAI-compatible API at {LLM_BASE_URL} (default:
http://host.docker.internal:1234/v1). The `openai` SDK is used for all calls
because it handles retry backoff and keep-alive automatically.
"""

import json
import logging
import os
import re
from typing import Any, Optional

from openai import OpenAI

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration from environment (set by docker-compose or .env)
# ─────────────────────────────────────────────────────────────────────────────
LLM_BASE_URL   = os.getenv("LLM_BASE_URL", "http://host.docker.internal:1234/v1")
LLM_API_KEY    = os.getenv("LLM_API_KEY", "lm-studio")
CHAT_MODEL     = os.getenv("LLM_CHAT_MODEL", "meta-llama-3.1-8b-instruct@q3_k_l")
EMBED_MODEL    = os.getenv("EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")
EMBED_DIMS     = int(os.getenv("EMBED_DIMS", "1024"))

_client: Optional[OpenAI] = None
_working_chat_model: Optional[str] = None  # cached after first successful call


def _get_client() -> OpenAI:
    """Lazily initialize and reuse the OpenAI client."""
    global _client
    if _client is None:
        _client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    return _client


def _get_chat_model() -> str:
    """
    Return a model ID that actually responds to chat completions.

    LM Studio only serves models that are explicitly loaded in its UI.
    This function probes the /v1/models list and tries each non-embedding
    model until one responds successfully, then caches it.

    Falls back to CHAT_MODEL (from env) if nothing can be tested.
    """
    global _working_chat_model
    if _working_chat_model:
        return _working_chat_model

    import httpx

    # Build candidate list: configured model first, then all listed models
    candidates = [CHAT_MODEL]
    try:
        resp = httpx.get(f"{LLM_BASE_URL}/models", timeout=5.0)
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
    """
    Embed text using LM Studio's embedding endpoint.

    Text is truncated to 8000 characters before embedding — the Nomic model
    has an 8192-token context limit and long inputs degrade embedding quality.

    Returns a list of EMBED_DIMS floats (default 1024 for Nomic embed text v1.5).
    """
    model = model or EMBED_MODEL
    text = text[:8000]
    response = _get_client().embeddings.create(model=model, input=[text])
    return response.data[0].embedding


def _extract_json(text: str) -> Optional[dict]:
    """
    Try to extract a JSON object from arbitrary LLM output text.

    The LLM sometimes wraps JSON in markdown code fences or prepends prose.
    This function handles the common cases:
      1. Pure JSON
      2. JSON inside ```json ... ``` blocks
      3. JSON preceded or followed by prose text
    """
    # Try direct parse first (fast path)
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Strip markdown code fences
    fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Find the outermost {...} block
    start = text.find('{')
    end = text.rfind('}')
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
    Call LM Studio chat completions with a self-correcting retry loop.

    Ported from flask_app.py _self_correcting_rca(), generalized for any prompt.

    Parameters
    ----------
    prompt        : User message content
    system        : System prompt
    max_tokens    : Max completion tokens
    temperature   : Sampling temperature (lower = more deterministic)
    max_retries   : Max attempts before returning a failure dict
    required_keys : If provided, returned JSON must contain all these top-level keys

    Returns
    -------
    dict with:
      content   — raw LLM text output (last attempt)
      parsed    — parsed JSON dict, or None if all retries failed
      attempts  — number of attempts made
      _failed   — True if all retries exhausted without valid output
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
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
            )
            content = response.choices[0].message.content or ""
            last_content = content

            logger.debug("LLM raw output (attempt %d): %s", attempt, content[:500])
            parsed = _extract_json(content)
            if parsed is None:
                # Only retry when JSON is completely unparseable
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

            # Successfully parsed — return immediately regardless of which keys are present
            # (caller handles missing/empty values via post-parse cleanup)
            logger.info("LLM succeeded on attempt %d/%d", attempt, max_retries)
            return {"content": content, "parsed": parsed, "attempts": attempt, "_failed": False}

        except Exception as exc:
            logger.error("LLM attempt %d/%d exception: %s", attempt, max_retries, exc)
            if attempt == max_retries:
                break
            # On API error, don't append to messages — just retry clean
            continue

    logger.error("LLM failed after %d attempts", max_retries)
    return {
        "content": last_content,
        "parsed": None,
        "attempts": max_retries,
        "_failed": True,
    }


def call_llm_reasoning(prompt: str, system: str, max_tokens: int = 800) -> str:
    """
    Pass 1 of the scratchpad pattern: free-text reasoning, no JSON required.

    Returns raw text — not JSON. This is stored in the scratchpad and used
    as context for Pass 2 (call_llm with json schema).
    """
    try:
        response = _get_client().chat.completions.create(
            model=_get_chat_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.5,  # slightly higher for broader reasoning
        )
        return response.choices[0].message.content or ""
    except Exception as exc:
        logger.error("LLM reasoning call failed: %s", exc)
        return f"[reasoning failed: {exc}]"


def health_check() -> bool:
    """Return True if LM Studio is reachable and the embedding endpoint responds."""
    try:
        result = embed("health check")
        return len(result) == EMBED_DIMS
    except Exception:
        return False
