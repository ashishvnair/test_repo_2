"""
pipeline/nodes/llm_analysis.py — Node 5: 15-Question Q&A Engine + 2 Unique Questions

Reads from connector:
  USE_LANGCHAIN_LLM, QA_BATCH_SIZE, QA_MAX_TOKENS

Two phases:
  Phase 1 — Answer RCA_QUESTIONS (Q01-Q15) in batches of QA_BATCH_SIZE via LLM
  Phase 2 — Generate 2 incident-specific unique questions + answers (Q16/Q17)

LLM backend is selected at runtime:
  USE_LANGCHAIN_LLM = True  → ai.llm.build_qa_chain(chat_model).ainvoke()
  USE_LANGCHAIN_LLM = False → mcp_client.call_tool("call_llm", ...)

State consumed:  pill_header, all_incidents, top_errors, window_str, app_id
State produced:  answers, unique_qa, sse_events
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ─── 15 Generic RCA Questions ──────────────────────────────────────────────────
RCA_QUESTIONS: list[tuple[str, str]] = [
    ("Q01", "What is the primary failing component and service?"),
    ("Q02", "What is the single root cause of the failure — the underlying condition that, if fixed, prevents recurrence?"),
    ("Q03", "How did the error first manifest — what was the triggering event?"),
    ("Q04", "What was the step-by-step sequence of events leading from normal operation to failure? List as ordered steps."),
    ("Q05", "Which error type occurred most frequently and why is it dominant over the others?"),
    ("Q06", "Are there authentication or authorization failures? What specifically is causing them?"),
    ("Q07", "Are there database connectivity or query performance issues? What specifically?"),
    ("Q08", "Are there memory, CPU, or resource exhaustion patterns? In which component?"),
    ("Q09", "Are there network timeout or latency issues? Between which components?"),
    ("Q10", "What is the blast radius — how many users, requests, or dependent services are affected?"),
    ("Q11", "Are errors correlated across multiple components? Describe the cascade and which error triggered which."),
    ("Q12", "What external dependencies or upstream services may have contributed to or amplified the failure?"),
    ("Q13", "What is the severity — critical / high / medium / low — and the justification for that rating?"),
    ("Q14", "What are the 3-4 immediate mitigation steps to stop ongoing damage right now?"),
    ("Q15", "What long-term architectural or configuration changes would permanently prevent this class of failure?"),
]

QA_SYSTEM = (
    "You are a senior Site Reliability Engineer. "
    "Answer questions concisely and technically — 2-4 sentences per question. "
    "Reference the actual error types, counts, and components from the data provided."
)


def _build_qa_prompt(pill_header: str, questions: list[tuple[str, str]]) -> str:
    """Build a compact multi-question prompt for one batch."""
    lines = [pill_header, "", "Answer each question (2-4 sentences each):", ""]
    for qid, qtxt in questions:
        lines.append(f"{qid}: {qtxt}")
    lines += ["", "Reply ONLY in this format — one answer per line, starting with the question ID:"]
    for qid, _ in questions:
        lines.append(f"{qid}: [your answer]")
    return "\n".join(lines)


def _build_unique_q_prompt(pill_header: str, context_answers: dict[str, str]) -> str:
    """Ask the LLM for 2 incident-specific questions + answers."""
    ctx_lines = []
    for qid in ["Q01", "Q02", "Q11"]:
        a = context_answers.get(qid, "")
        if a:
            ctx_lines.append(f"  {qid}: {a[:150]}")
    ctx = "\n".join(ctx_lines) or "  (no prior context)"
    already = (
        "primary component, root cause, triggering event, event sequence, dominant error, "
        "auth issues, database issues, memory issues, network issues, blast radius, "
        "error cascade, external dependencies, severity, immediate fixes, long-term fixes"
    )
    return (
        f"{pill_header}\n\n"
        f"Prior analysis context:\n{ctx}\n\n"
        f"Already covered: {already}.\n\n"
        "Suggest exactly 2 questions that are:\n"
        "  1. NOT already covered above\n"
        "  2. SPECIFIC to this incident's unique error pattern mix\n"
        "  3. Would reveal critical additional insight\n\n"
        "Then answer each question (3-4 sentences).\n\n"
        "Reply ONLY in this exact format:\n"
        "Q16: [unique question]\n"
        "A16: [your answer]\n"
        "Q17: [unique question]\n"
        "A17: [your answer]"
    )


def _parse_qa_response(text: str, expected_ids: list[str]) -> dict[str, str]:
    """Parse 'Q01: answer\\nQ02: answer...' LLM response into {id: answer}."""
    result: dict[str, str] = {}
    parts = re.split(r'\b([QA]\d+)\s*[:.]\s*', text)
    i = 1
    while i + 1 < len(parts):
        qid  = parts[i].strip()
        body = parts[i + 1].strip()
        if qid:
            result[qid] = body
        i += 2
    for eid in expected_ids:
        if eid not in result:
            result[eid] = ""
    return result


async def _call_llm_mcp(prompt: str, system: str, max_tokens: int) -> str:
    """Call LLM via MCP tool — returns raw text content."""
    from api_server import mcp_client
    result = await mcp_client.call_tool("call_llm", {
        "prompt":       prompt,
        "system":       system,
        "max_tokens":   max_tokens,
        "is_reasoning": True,
    })
    return result.get("content", "").strip()


async def _call_llm_langchain(chat_model: Any, prompt: str, system: str, max_tokens: int) -> str:
    """Call LLM via LangChain ChatOpenAI chain — returns raw text content."""
    from ai.llm import build_qa_chain
    chain = build_qa_chain(chat_model, system=system, max_tokens=max_tokens)
    return await chain.ainvoke({"prompt": prompt})


async def llm_analysis_node(state: dict) -> dict:
    """LangGraph node — run 15+2 Q&A engine, return answers + unique_qa."""
    import connector

    pill_header   = state.get("pill_header", "")
    all_incidents = state.get("all_incidents", [])
    top_errors    = state.get("top_errors", [])
    events: list[dict] = list(state.get("sse_events", []))

    total_q      = len(RCA_QUESTIONS)
    batch_size   = int(connector.QA_BATCH_SIZE)
    max_tokens   = int(connector.QA_MAX_TOKENS)
    use_lc       = bool(connector.USE_LANGCHAIN_LLM)

    # Resolve LangChain model if needed
    chat_model: Any = None
    if use_lc:
        try:
            chat_model = connector.get_chat_model()
        except Exception as exc:
            logger.warning("LangChain model init failed (%s), falling back to MCP", exc)
            use_lc = False

    events.append({
        "step":   "llm_analysis",
        "status": "running",
        "data": {
            "message":        f"Answering {total_q} diagnostic questions about {len(all_incidents)} error patterns…",
            "phase":          "init",
            "total_questions": total_q,
        },
    })

    # ── Phase 1: Q01-Q15 in batches ───────────────────────────────────────────
    answers: dict[str, str] = {}
    q_batches = [
        RCA_QUESTIONS[i:i + batch_size]
        for i in range(0, len(RCA_QUESTIONS), batch_size)
    ]
    total_batches = len(q_batches)

    for bidx, batch in enumerate(q_batches):
        batch_ids   = [qid for qid, _ in batch]
        batch_start = bidx * batch_size + 1
        batch_end   = batch_start + len(batch) - 1

        events.append({
            "step":   "llm_qa",
            "status": "running",
            "data": {
                "batch":     bidx + 1,
                "total":     total_batches,
                "qids":      batch_ids,
                "questions": [{"id": qid, "text": qtxt} for qid, qtxt in batch],
                "qrange":    f"Q{batch_start:02d}–Q{batch_end:02d}",
            },
        })

        try:
            prompt = _build_qa_prompt(pill_header, batch)
            if use_lc and chat_model:
                raw_text = await _call_llm_langchain(chat_model, prompt, QA_SYSTEM, max_tokens)
            else:
                raw_text = await _call_llm_mcp(prompt, QA_SYSTEM, max_tokens)

            batch_answers = _parse_qa_response(raw_text, batch_ids)
            answers.update(batch_answers)

            events.append({
                "step":   "llm_qa",
                "status": "done",
                "data": {
                    "batch":     bidx + 1,
                    "total":     total_batches,
                    "qids":      batch_ids,
                    "questions": [{"id": qid, "text": qtxt} for qid, qtxt in batch],
                    "answers":   {qid: batch_answers.get(qid, "") for qid in batch_ids},
                    "qrange":    f"Q{batch_start:02d}–Q{batch_end:02d}",
                },
            })

        except Exception as exc:
            logger.warning("QA batch %d failed: %s", bidx + 1, exc)
            events.append({
                "step":   "llm_qa",
                "status": "error",
                "data":   {"batch": bidx + 1, "total": total_batches, "qids": batch_ids, "message": str(exc)[:120]},
            })

    # ── Phase 2: Q16/Q17 unique questions ─────────────────────────────────────
    events.append({
        "step":   "llm_unique_q",
        "status": "running",
        "data":   {"message": "Generating 2 incident-specific unique questions…"},
    })
    unique_qa: list[dict] = []
    try:
        prompt = _build_unique_q_prompt(pill_header, answers)
        if use_lc and chat_model:
            uq_text = await _call_llm_langchain(chat_model, prompt, QA_SYSTEM, 350)
        else:
            uq_text = await _call_llm_mcp(prompt, QA_SYSTEM, 350)

        uq_parsed = _parse_qa_response(uq_text, ["Q16", "A16", "Q17", "A17"])
        if uq_parsed.get("Q16") and uq_parsed.get("A16"):
            unique_qa.append({"id": "Q16", "question": uq_parsed["Q16"], "answer": uq_parsed["A16"]})
        if uq_parsed.get("Q17") and uq_parsed.get("A17"):
            unique_qa.append({"id": "Q17", "question": uq_parsed["Q17"], "answer": uq_parsed["A17"]})

        events.append({
            "step":   "llm_unique_q",
            "status": "done",
            "data":   {"unique_qa": unique_qa, "count": len(unique_qa)},
        })
    except Exception as exc:
        logger.warning("Unique questions failed: %s", exc)
        events.append({
            "step":   "llm_unique_q",
            "status": "error",
            "data":   {"message": str(exc)[:120]},
        })

    return {
        "answers":    answers,
        "unique_qa":  unique_qa,
        "sse_events": events,
    }
