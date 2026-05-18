"""
report_template.py — Jinja2 RCA report renderer (Q&A edition).

Sections
--------
 1. Executive Summary        — severity badge, one-liner, app / window / stats
 2. Problem Statement        — what failed (from Q01 + Q03)
 3. Blast Radius             — impact scope (from Q10)
 4. Root Cause Analysis      — the fundamental cause (from Q02)
 5. Contributing Factors     — aggravating conditions (from Q06–Q09, Q12)
 6. Timeline of Events       — ordered sequence (from Q04)
 7. Causal Chain             — visual node chain (from Q11)
 8. Recommended Fix Steps    — immediate mitigations (from Q14)
 9. Long-term Prevention     — architectural fixes (from Q15)
10. Verification Plan        — checklist to confirm fix worked
11. Unique Insights          — Q16 + Q17 (incident-specific unique questions)
12. Error Breakdown          — top_errors table from log_pill
13. Full Q&A Reference       — collapsible: all 17 questions + answers
"""

from jinja2 import Environment, BaseLoader

_env = Environment(loader=BaseLoader(), autoescape=True)
_env.filters["commaformat"] = lambda v: f"{int(v):,}" if v else "0"

_TEMPLATE_SRC = r"""
<div class="rca-report">

  {# ── 1. EXECUTIVE SUMMARY ──────────────────────────────────────────────── #}
  <div class="rca-exec rca-exec--{{ severity }}">
    <div class="rca-exec-top">
      <span class="rca-sev-badge rca-sev--{{ severity }}">{{ severity | upper }} SEVERITY</span>
      <span class="rca-exec-app">{{ app_id }}</span>
      <span class="rca-exec-window">{{ window }}</span>
    </div>
    <p class="rca-exec-one-liner">{{ summary }}</p>
    <div class="rca-exec-stats">
      <span><strong>{{ total_raw_lines | commaformat }}</strong> log lines</span>
      <span class="rca-dot">·</span>
      <span><strong>{{ unique_error_patterns }}</strong> unique patterns</span>
      {% if top_error_types %}
      <span class="rca-dot">·</span>
      <span>Top errors: <strong>{{ top_error_types | join(", ") }}</strong></span>
      {% endif %}
    </div>
  </div>

  {# ── 2. PROBLEM STATEMENT ──────────────────────────────────────────────── #}
  {% if problem_statement %}
  <div class="rca-sec">
    <div class="rca-sec-hdr">📋 Problem Statement</div>
    <div class="rca-sec-body rca-problem-body">
      {% for para in problem_statement_paras %}
      <p>{{ para }}</p>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  {# ── 3. BLAST RADIUS ───────────────────────────────────────────────────── #}
  {% if blast_radius %}
  <div class="rca-sec">
    <div class="rca-sec-hdr">💥 Blast Radius</div>
    <div class="rca-sec-body">
      <p class="rca-blast-text">{{ blast_radius }}</p>
    </div>
  </div>
  {% endif %}

  {# ── 4. ROOT CAUSE ─────────────────────────────────────────────────────── #}
  <div class="rca-sec">
    <div class="rca-sec-hdr">🔍 Root Cause Analysis</div>
    <div class="rca-sec-body rca-root-cause-body">
      {% for para in root_cause_paras %}
      <p>{{ para }}</p>
      {% endfor %}
    </div>
  </div>

  {# ── 5. CONTRIBUTING FACTORS ───────────────────────────────────────────── #}
  {% if contributing_factors %}
  <div class="rca-sec">
    <div class="rca-sec-hdr">⚠️ Contributing Factors</div>
    <div class="rca-sec-body">
      <ul class="rca-bullet-list">
        {% for factor in contributing_factors %}
        <li>{{ factor }}</li>
        {% endfor %}
      </ul>
    </div>
  </div>
  {% endif %}

  {# ── 6. TIMELINE OF EVENTS ─────────────────────────────────────────────── #}
  {% if timeline %}
  <div class="rca-sec">
    <div class="rca-sec-hdr">🕐 Timeline of Events</div>
    <div class="rca-sec-body">
      <ol class="rca-timeline-ol">
        {% for event in timeline %}
        <li>{{ event }}</li>
        {% endfor %}
      </ol>
    </div>
  </div>
  {% endif %}

  {# ── 7. CAUSAL CHAIN ───────────────────────────────────────────────────── #}
  {% if causal_chain %}
  <div class="rca-sec">
    <div class="rca-sec-hdr">⛓ Causal Chain</div>
    <div class="rca-sec-body">
      <div class="rca-chain">
        {% for node in causal_chain %}
        {% if not loop.first %}<span class="rca-chain-arrow">→</span>{% endif %}
        <span class="rca-chain-node">{{ node }}</span>
        {% endfor %}
      </div>
      {% if causal_chain_text %}
      <p class="rca-chain-detail">{{ causal_chain_text }}</p>
      {% endif %}
    </div>
  </div>
  {% endif %}

  {# ── 8. RECOMMENDED FIX STEPS ──────────────────────────────────────────── #}
  {% if fix_steps %}
  <div class="rca-sec">
    <div class="rca-sec-hdr">🛠 Recommended Fix Steps</div>
    <div class="rca-sec-body">
      <ol class="rca-fix-ol">
        {% for step in fix_steps %}
        <li>{{ step }}</li>
        {% endfor %}
      </ol>
    </div>
  </div>
  {% endif %}

  {# ── 9. LONG-TERM PREVENTION ───────────────────────────────────────────── #}
  {% if long_term_fixes %}
  <div class="rca-sec">
    <div class="rca-sec-hdr">🏗 Long-term Prevention</div>
    <div class="rca-sec-body">
      <ol class="rca-fix-ol rca-longterm-ol">
        {% for fix in long_term_fixes %}
        <li>{{ fix }}</li>
        {% endfor %}
      </ol>
    </div>
  </div>
  {% endif %}

  {# ── 10. VERIFICATION PLAN ─────────────────────────────────────────────── #}
  {% if verification_steps %}
  <div class="rca-sec">
    <div class="rca-sec-hdr">✅ Verification Plan</div>
    <div class="rca-sec-body">
      <ul class="rca-verify-list">
        {% for check in verification_steps %}
        <li>{{ check }}</li>
        {% endfor %}
      </ul>
    </div>
  </div>
  {% endif %}

  {# ── 11. UNIQUE INSIGHTS (Q16 / Q17) ──────────────────────────────────── #}
  {% if unique_qa %}
  <div class="rca-sec rca-unique-sec">
    <div class="rca-sec-hdr">
      ✨ Unique Insights
      <span class="rca-sec-meta">Incident-specific questions generated by AI</span>
    </div>
    <div class="rca-sec-body">
      {% for uq in unique_qa %}
      <div class="rca-uq-card">
        <div class="rca-uq-header">
          <span class="rca-uq-badge">{{ uq.id }}</span>
          <span class="rca-uq-question">{{ uq.question }}</span>
        </div>
        <div class="rca-uq-answer">{{ uq.answer }}</div>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  {# ── 12. ERROR BREAKDOWN ───────────────────────────────────────────────── #}
  {% if top_errors %}
  <div class="rca-sec">
    <div class="rca-sec-hdr">
      📊 Error Breakdown
      <span class="rca-sec-meta">{{ unique_error_patterns }} patterns · {{ total_raw_lines | commaformat }} raw lines · {{ window }}</span>
    </div>
    <div class="rca-sec-body">
      <table class="rca-tbl">
        <thead>
          <tr><th>Type</th><th>Count</th><th>Sample</th></tr>
        </thead>
        <tbody>
          {% for e in top_errors %}
          <tr>
            <td><span class="rca-type-tag rca-type-{{ e.type }}">{{ e.type }}</span></td>
            <td class="rca-tbl-count">{{ e.count | commaformat }}</td>
            <td class="rca-tbl-sample">{{ e.sample }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% endif %}

  {# ── 13. FULL Q&A REFERENCE (collapsible) ─────────────────────────────── #}
  {% if all_qa %}
  <details class="rca-sec rca-details">
    <summary class="rca-sec-hdr rca-details-summary">
      💬 Full Q&amp;A Reference
      <span class="rca-dim">({{ all_qa | length }} questions — click to expand)</span>
    </summary>
    <div class="rca-sec-body rca-qa-grid">
      {% for item in all_qa %}
      <div class="rca-qa-item {% if item.id in ('Q16','Q17') %}rca-qa-unique{% endif %}">
        <div class="rca-qa-q">
          <span class="rca-qa-id">{{ item.id }}</span>
          {{ item.question }}
        </div>
        <div class="rca-qa-a">{{ item.answer or '—' }}</div>
      </div>
      {% endfor %}
    </div>
  </details>
  {% endif %}

</div>
"""

_template = _env.from_string(_TEMPLATE_SRC)


def _split_paras(text: str) -> list:
    if not text:
        return []
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paras) == 1:
        paras = [p.strip() for p in text.split("\n") if p.strip()]
    return paras or [text.strip()]


def _clean_list(raw) -> list:
    if isinstance(raw, str):
        items = [s.strip() for s in raw.split("\n") if s.strip()]
    elif isinstance(raw, list):
        items = [str(s).strip() for s in raw if str(s).strip()]
    else:
        items = []
    return [i for i in items if i]


def render_report(
    report: dict,
    log_pill: dict,
    reasoning_text: str = "",
) -> str:
    top_errors_list = log_pill.get("top_errors", [])

    severity = str(report.get("severity", "medium")).lower()
    if severity not in ("high", "medium", "low", "critical"):
        severity = "medium"
    if severity == "critical":
        severity = "high"   # map critical → high for CSS class

    summary = report.get("summary", "") or report.get("incident_summary", "")
    if not summary:
        summary = (
            f"{log_pill.get('unique_error_patterns', 0)} unique error patterns detected "
            f"in {log_pill.get('app_id', 'app')} over {log_pill.get('window', 'the analysis window')}."
        )

    problem_statement       = report.get("problem_statement", "")
    problem_statement_paras = _split_paras(problem_statement)

    root_cause_raw   = report.get("root_cause", "") or ""
    root_cause_paras = _split_paras(root_cause_raw) or ["Analysis unavailable."]

    blast_radius         = report.get("blast_radius", "")
    contributing_factors = _clean_list(report.get("contributing_factors", []))
    timeline             = _clean_list(report.get("timeline", []))
    fix_steps            = _clean_list(report.get("fix_steps", []))
    long_term_fixes      = _clean_list(report.get("long_term_fixes", []))
    verification_steps   = _clean_list(report.get("verification_steps", []))

    causal_chain = report.get("causal_chain", [])
    if isinstance(causal_chain, str):
        causal_chain = [c.strip() for c in causal_chain.split("→") if c.strip()]
    causal_chain = [str(c).strip() for c in causal_chain if str(c).strip()]

    causal_chain_text = report.get("causal_chain_text", "")

    unique_qa = report.get("unique_qa", [])
    all_qa    = report.get("all_qa", [])

    # Deduplicated error types for exec summary line
    error_types = report.get("error_types", [])
    if not error_types and top_errors_list:
        error_types = []
        for e in top_errors_list:
            t = e.get("type", "")
            if t and t != "UNKNOWN" and t not in error_types:
                error_types.append(t)
            if len(error_types) >= 4:
                break
    seen_et: list = []
    for et in error_types:
        if et and et not in seen_et:
            seen_et.append(et)
    error_types = seen_et

    ctx = {
        "severity":                severity,
        "summary":                 summary,
        "problem_statement":       problem_statement,
        "problem_statement_paras": problem_statement_paras,
        "blast_radius":            blast_radius,
        "root_cause_paras":        root_cause_paras,
        "contributing_factors":    contributing_factors,
        "timeline":                timeline,
        "causal_chain":            causal_chain,
        "causal_chain_text":       causal_chain_text,
        "fix_steps":               fix_steps,
        "long_term_fixes":         long_term_fixes,
        "verification_steps":      verification_steps,
        "unique_qa":               unique_qa,
        "all_qa":                  all_qa,
        "top_error_types":         error_types[:4],
        "reasoning_text":          reasoning_text.strip() if reasoning_text else "",
        "app_id":                  log_pill.get("app_id", ""),
        "window":                  log_pill.get("window", ""),
        "total_raw_lines":         log_pill.get("total_raw_lines", 0),
        "unique_error_patterns":   log_pill.get("unique_error_patterns", 0),
        "top_errors":              top_errors_list,
    }

    return _template.render(**ctx)
