"""
pipeline/nodes/report_assembly.py — Node 6: Map Q&A → report fields + render HTML

No LLM calls — pure Python mapping from Q&A answers to report dict sections.
Calls render_report() from api_server.report_template for HTML.

Reads from connector:
  ENABLE_HTML_REPORT

State consumed:  answers, unique_qa, log_pill, top_errors, window_str, app_id, all_incidents
State produced:  report, html, sse_events
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def _clean_answer(text: str) -> str:
    """Strip markdown artefacts and leading Q-ID labels."""
    text = re.sub(r'^\s*[QA]\d+\s*[:.]\s*', '', text).strip()
    text = re.sub(r'\*+', '', text).strip()
    return text


def _split_to_steps(text: str) -> list[str]:
    """Split prose into step strings. Handles numbered lists, bullets, sentences."""
    if not text:
        return []
    # Numbered list markers (1. / 1) / Step 1:)
    numbered = re.split(r'(?:^|\n)\s*(?:step\s*)?\d+[.):\s]+', text, flags=re.IGNORECASE)
    steps = [s.strip() for s in numbered if len(s.strip()) > 15]
    if len(steps) >= 2:
        return steps[:6]
    # Bullet markers
    bulleted = re.split(r'(?:^|\n)\s*[-•*▸]\s+', text)
    steps = [s.strip() for s in bulleted if len(s.strip()) > 15]
    if len(steps) >= 2:
        return steps[:6]
    # Sentence boundary fallback
    sentences = re.split(r'(?<=[.!?])\s+', text)
    return [s.strip() for s in sentences if len(s.strip()) > 15][:6]


def _extract_severity(text: str) -> str:
    """Extract severity level word from a prose answer."""
    m = re.search(r'\b(critical|high|medium|low)\b', text, re.IGNORECASE)
    return m.group(1).lower() if m else "medium"


async def report_assembly_node(state: dict) -> dict:
    """LangGraph node — map Q&A answers to report fields + render HTML.

    Handles two cases:
      is_known=True  → retrieve report from best_hit, re-attach log_pill + html
      is_known=False → build report dict from Q&A answers, render HTML
    """
    import connector
    from api_server.report_template import render_report

    answers       = state.get("answers", {})
    unique_qa     = state.get("unique_qa", [])
    log_pill      = state.get("log_pill", {})
    top_errors    = state.get("top_errors", [])
    window_str    = state.get("window_str", "")
    app_id        = state.get("app_id", "")
    all_incidents = state.get("all_incidents", [])
    is_known      = state.get("is_known", False)
    best_hit      = state.get("best_hit", {})
    events: list[dict] = list(state.get("sse_events", []))

    # ── Known issue short-circuit ─────────────────────────────────────────────
    if is_known and best_hit:
        events.append({
            "step":   "llm_analysis",
            "status": "running",
            "data":   {"message": "Known issue — retrieving cached report…", "phase": "known"},
        })
        report = dict(best_hit.get("report", {}))
        report["log_pill"] = log_pill
        html = render_report(report, log_pill, reasoning_text="") if connector.ENABLE_HTML_REPORT else ""
        report["html"] = html
        events.append({"step": "llm_analysis", "status": "done", "data": "Known issue matched"})
        return {
            "report":     report,
            "html":       html,
            "sse_events": events,
        }

    events.append({
        "step":   "llm_synthesis",
        "status": "running",
        "data":   {"message": "Assembling final report from Q&A answers…"},
    })

    def _ans(qid: str) -> str:
        return _clean_answer(answers.get(qid, ""))

    report: dict = {}

    # ── problem_statement — Q01 + Q03 ─────────────────────────────────────────
    ps_parts = [p for p in [_ans("Q01"), _ans("Q03")] if len(p) > 15]
    report["problem_statement"] = "\n\n".join(ps_parts) or (
        f"App '{app_id}' is experiencing {len(all_incidents)} distinct "
        f"error patterns over {window_str}."
    )

    # ── root_cause — Q02 ──────────────────────────────────────────────────────
    report["root_cause"] = _ans("Q02") or (
        f"Multiple error patterns detected across {len(all_incidents)} unique fingerprints. "
        f"Top patterns: {', '.join(e['type'] for e in top_errors[:3])}."
    )

    # ── summary (exec one-liner) — first sentence of Q02 ─────────────────────
    q02_first = re.split(r'(?<=[.!?])\s+', _ans("Q02") or "")
    report["summary"] = q02_first[0] if q02_first else (
        f"{len(all_incidents)} error patterns in {window_str} — "
        f"top: {top_errors[0]['type'] if top_errors else 'UNKNOWN'}"
    )

    # ── dominant_error — Q05 ──────────────────────────────────────────────────
    report["dominant_error"] = _ans("Q05")

    # ── contributing_factors — Q06–Q09 + Q12 ─────────────────────────────────
    contributing_answers = [_ans(qid) for qid in ["Q06", "Q07", "Q08", "Q09", "Q12"]]
    report["contributing_factors"] = [a for a in contributing_answers if len(a) > 15]
    if not report["contributing_factors"]:
        report["contributing_factors"] = [
            f"High occurrence of {top_errors[0]['type']} errors" if top_errors
            else "Multiple error types detected simultaneously",
            "Possible cascading failures between dependent components",
        ]

    # ── timeline — Q04 split into steps ───────────────────────────────────────
    report["timeline"] = _split_to_steps(_ans("Q04")) or [
        f"Steady-state errors accumulate in {window_str} window",
        f"{top_errors[0]['type'] if top_errors else 'Primary'} errors begin",
        "Secondary error types cascade from primary failure",
        "Pattern stabilises — errors persist until root cause addressed",
    ]

    # ── blast_radius — Q10 ────────────────────────────────────────────────────
    report["blast_radius"] = _ans("Q10")

    # ── causal_chain_text + causal_chain nodes — Q11 ──────────────────────────
    causal_text = _ans("Q11")
    report["causal_chain_text"] = causal_text
    arrow_nodes: list[str] = []
    for chunk in re.split(r'[→\-]+>', causal_text):
        node = re.sub(r'[^\w_]', ' ', chunk).split()[0] if chunk.strip() else ""
        if node and len(node) > 1 and node not in arrow_nodes:
            arrow_nodes.append(node)
    if len(arrow_nodes) >= 2:
        report["causal_chain"] = arrow_nodes[:5]
    else:
        seen_cc: list = []
        for e in top_errors:
            t = e["type"]
            if t not in seen_cc:
                seen_cc.append(t)
            if len(seen_cc) >= 4:
                break
        report["causal_chain"] = seen_cc

    # ── severity — Q13 ────────────────────────────────────────────────────────
    report["severity"] = _extract_severity(_ans("Q13")) or "medium"

    # ── fix_steps — Q14 ───────────────────────────────────────────────────────
    report["fix_steps"] = _split_to_steps(_ans("Q14")) or [
        f"Investigate top error pattern: {top_errors[0]['type'] if top_errors else 'UNKNOWN'}",
        "Check database connectivity and authentication configuration",
        "Review upstream service health and circuit-breaker status",
    ]

    # ── long_term_fixes — Q15 ─────────────────────────────────────────────────
    report["long_term_fixes"] = _split_to_steps(_ans("Q15")) or [
        "Implement connection pooling and circuit-breakers for database access",
        "Add structured alerting on error-rate thresholds per component",
        "Review authentication token TTL and refresh logic",
    ]

    # ── verification_steps — derived from top error type ─────────────────────
    report["verification_steps"] = [
        f"Confirm {top_errors[0]['type']} error rate drops to zero in Splunk after applying fix"
        if top_errors else "Confirm primary error rate drops to zero",
        "Run end-to-end smoke test across all affected components",
        "Monitor for 15 minutes — zero recurrence confirms fix is effective",
    ]

    # ── category — from top error's category field (needed for pgvector scoring) ──
    report["category"] = top_errors[0]["category"] if top_errors else "unknown"

    # ── error_types — deduplicated from log_pill ──────────────────────────────
    seen_et: list = []
    for e in top_errors:
        t = e.get("type", "")
        if t and t not in seen_et:
            seen_et.append(t)
    report["error_types"] = seen_et[:6] or ["UNKNOWN"]

    # ── unique Q&A (Q16/Q17) ─────────────────────────────────────────────────
    report["unique_qa"] = unique_qa

    # ── Full Q&A reference (all 17 items) ─────────────────────────────────────
    from pipeline.nodes.llm_analysis import RCA_QUESTIONS
    all_qa_list = [
        {"id": qid, "question": qtxt, "answer": _ans(qid)}
        for qid, qtxt in RCA_QUESTIONS
    ] + unique_qa
    report["all_qa"] = all_qa_list

    # ── reasoning_text for Full Q&A collapsible ───────────────────────────────
    reasoning_text = "\n\n".join(
        f"{qid}: {qtxt}\n→ {_ans(qid)}"
        for qid, qtxt in RCA_QUESTIONS
        if _ans(qid)
    )
    if unique_qa:
        for uq in unique_qa:
            reasoning_text += f"\n\n{uq['id']}: {uq['question']}\n→ {uq['answer']}"

    # ── Attach log_pill ───────────────────────────────────────────────────────
    report["log_pill"] = log_pill
    if "error_type" not in report and top_errors:
        report["error_type"] = top_errors[0]["type"]

    # ── Render HTML (skip if disabled in connector) ───────────────────────────
    html = ""
    if connector.ENABLE_HTML_REPORT:
        html = render_report(report, log_pill, reasoning_text)
    report["html"] = html

    events.append({
        "step":   "llm_synthesis",
        "status": "done",
        "data":   {"message": "Report assembled from Q&A"},
    })
    events.append({
        "step":   "llm_analysis",
        "status": "done",
        "data":   "AI analysis complete",
    })

    return {
        "report":     report,
        "html":       html,
        "sse_events": events,
    }
