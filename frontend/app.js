'use strict';

// ── State ─────────────────────────────────────────────────────────────────
let _results  = [];  // [{incident, report, embedding, status: 'known'|'new'|'fail'}]
let _running  = false;
let _logCount = 0;

// ── Page navigation ───────────────────────────────────────────────────────
function showPage(name) {
  document.body.className = 'page-' + name;
  // Scroll result panels to top on transition
  if (name === 'results') {
    const main = document.querySelector('.results-main');
    const sidebar = document.querySelector('.results-sidebar');
    if (main)    main.scrollTop    = 0;
    if (sidebar) sidebar.scrollTop = 0;
  }
}

function cancelRCA() {
  _running = false;
  const btn = document.getElementById('btn-run');
  if (btn) { btn.disabled = false; btn.textContent = '▶ Run RCA'; }
  showPage('control');
}

function newAnalysis() {
  showPage('control');
}

// ── Slider ────────────────────────────────────────────────────────────────
const slider = document.getElementById('time-slider');
const sliderLabel = document.getElementById('slider-label');

function fmtMinutes(m) {
  m = parseInt(m, 10);
  if (m < 60)  return m + ' min';
  if (m < 120) return '1 h';
  if (m % 60 === 0) return (m / 60) + ' h';
  return (m / 60).toFixed(1) + ' h';
}

slider.addEventListener('input', () => {
  sliderLabel.textContent = fmtMinutes(slider.value);
});

// ── Splunk status polling ─────────────────────────────────────────────────
const THRESHOLD = 100_000;
let _lastCount = 0;
let _lastCountTime = 0;

async function pollSplunkStats() {
  const label = document.getElementById('status-refresh-label');
  try {
    const d = await (await fetch('/api/splunk/stats')).json();

    const count = d.event_count || 0;
    document.getElementById('stat-lines').textContent =
      count >= 1_000_000 ? (count / 1_000_000).toFixed(2) + 'M'
      : count >= 1_000   ? (count / 1_000).toFixed(1) + 'K'
      : count.toString();

    const gb = d.size_gb || 0;
    document.getElementById('stat-gb').textContent =
      gb < 0.01 ? (d.size_mb || 0).toFixed(0) + ' MB'
      : gb.toFixed(3) + ' GB';

    const now = Date.now();
    if (_lastCountTime > 0 && now > _lastCountTime) {
      const elapsed_min = (now - _lastCountTime) / 60000;
      const delta = count - _lastCount;
      const rate = delta > 0 ? Math.round(delta / elapsed_min) : 0;
      document.getElementById('stat-rate').textContent =
        rate >= 1000 ? (rate / 1000).toFixed(1) + 'K' : rate.toString();
    }
    _lastCount = count;
    _lastCountTime = now;

    const pct = Math.min(100, Math.round((count / THRESHOLD) * 100));
    const bar  = document.getElementById('threshold-bar');
    const pctEl = document.getElementById('threshold-pct');
    bar.style.width  = pct + '%';
    pctEl.textContent = pct + '%';

    const isReady = count >= THRESHOLD;
    bar.className = 'threshold-bar-fill' + (isReady ? ' ready' : '');
    document.getElementById('stat-ready').textContent  = isReady ? '✅' : '⏳';
    document.getElementById('stat-ready').title        = isReady
      ? `${count.toLocaleString()} lines — ready for CSV export`
      : `Need ${(THRESHOLD - count).toLocaleString()} more lines`;

    label.textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch {
    label.textContent = 'Splunk unreachable';
  }
}

// ── Health check ──────────────────────────────────────────────────────────
async function checkHealth() {
  const dot   = document.getElementById('health-dot');
  const label = document.getElementById('health-label');
  try {
    const r = await fetch('/api/health');
    if (r.ok) {
      const d = await r.json();
      const ok = d.status === 'ok';
      dot.className = 'status-dot ' + (ok ? 'ok' : 'err');
      label.textContent = ok ? 'online' : 'degraded';
    } else {
      throw new Error('non-ok');
    }
  } catch {
    dot.className = 'status-dot err';
    label.textContent = 'offline';
  }
}

// ── Load apps ─────────────────────────────────────────────────────────────
let _apps = [];   // cached app registry — used by similar-reports panel for name lookup

async function loadApps() {
  const sel = document.getElementById('app-select');
  try {
    const apps = await (await fetch('/api/apps')).json();
    if (!Array.isArray(apps) || apps.length === 0) {
      sel.innerHTML = '<option value="">No apps registered</option>';
      return;
    }
    _apps = apps;
    sel.innerHTML = apps.map(a =>
      `<option value="${esc(a.app_id)}">${esc(a.app_name || a.app_id)}</option>`
    ).join('');
    updateFetchLabel();
  } catch (e) {
    sel.innerHTML = '<option value="">Error loading apps</option>';
  }
}

function updateFetchLabel() {
  const src = document.getElementById('source-select').value;
  const el = document.getElementById('label-fetch_logs');
  if (el) el.textContent = 'Fetch logs from ' + (src === 'loki' ? 'Loki' : 'Splunk');
}
document.getElementById('source-select').addEventListener('change', updateFetchLabel);

// ── Run RCA ───────────────────────────────────────────────────────────────
// _similarReports stores the last similar_found hits so "View" can access them
let _similarReports = [];

async function runRCA(skipVectorCheck = false) {
  if (_running) return;

  const appId   = document.getElementById('app-select').value;
  const source  = document.getElementById('source-select').value;
  const minutes = parseInt(slider.value, 10);

  if (!appId) { alert('Please select an application.'); return; }

  _running  = true;
  _results  = [];
  _logCount = 0;
  _similarReports = [];

  const btn = document.getElementById('btn-run');
  btn.disabled = true;
  btn.textContent = '⏳ Running…';

  // Reset all steps, clear analysis feed, and transition to pipeline page
  ['fetch_logs','clean_logs','log_pill','vector_check','llm_analysis'].forEach(resetStep);
  document.getElementById('summary-bar').classList.remove('visible');
  document.getElementById('results').innerHTML = '';
  document.getElementById('sidebar-content').innerHTML = '';
  hideFeed();
  showPage('pipeline');

  try {
    const resp = await fetch('/api/rca/process', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        app_id:             appId,
        since_seconds:      minutes * 60,
        source:             source,
        skip_vector_check:  skipVectorCheck,
      }),
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      const lines = buf.split('\n');
      buf = lines.pop();

      for (const line of lines) {
        const t = line.trim();
        if (!t.startsWith('data:')) continue;
        const raw = t.slice(5).trim();
        if (!raw) continue;
        try {
          const evt = JSON.parse(raw);
          handleSSE(evt);
        } catch { /* ignore malformed */ }
      }
    }
  } catch (e) {
    ['fetch_logs','clean_logs','log_pill','vector_check','llm_analysis'].forEach(k => {
      const el = document.getElementById('step-' + k);
      if (el && !el.classList.contains('done')) setStep(k, 'error', 'Pipeline error: ' + e.message);
    });
  } finally {
    _running = false;
    btn.disabled = false;
    btn.textContent = '▶ Run RCA';
  }
}

// ── SSE handler ───────────────────────────────────────────────────────────
function handleSSE(evt) {
  const { step, status, data } = evt;

  if (step === 'fetch_logs') {
    if (status === 'running') setStep('fetch_logs', 'running', typeof data === 'string' ? data : 'Fetching logs…');
    if (status === 'done') {
      const count = data && data.count != null ? data.count : (Array.isArray(data) ? data.length : 0);
      _logCount = count;
      setStep('fetch_logs', 'done', `${Number(count).toLocaleString()} log lines fetched`);
    }
    if (status === 'error') setStep('fetch_logs', 'error', data || 'Failed');
  }

  else if (step === 'clean_logs') {
    if (status === 'running') setStep('clean_logs', 'running', 'Deduplicating unique error patterns…');
    if (status === 'done') {
      const msg = data && data.message ? data.message
                : (data && data.count != null ? `${data.count} unique patterns` : 'Deduplicated');
      setStep('clean_logs', 'done', msg);
    }
    if (status === 'error') setStep('clean_logs', 'error', data || 'Failed');
  }

  else if (step === 'log_pill') {
    if (status === 'running') setStep('log_pill', 'running', 'Building compact log pill…');
    if (status === 'done') {
      const count = data && data.count != null ? data.count
                  : (data && data.incidents ? data.incidents.length : 0);
      setStep('log_pill', 'done', `Log pill built — top ${Math.min(count, 15)} of ${count} pattern${count !== 1 ? 's' : ''}`);
    }
    if (status === 'error') setStep('log_pill', 'error', data || 'Failed');
  }

  else if (step === 'vector_check') {
    if (status === 'running') setStep('vector_check', 'running', 'Searching vector DB…');
    if (status === 'done') {
      const known = data && data.known_count != null ? data.known_count
                  : (data && data.results ? data.results.filter(r => r.status === 'known').length : 0);
      const newC  = data && data.new_count  != null ? data.new_count
                  : (data && data.results ? data.results.filter(r => r.status !== 'known').length : 0);
      setStep('vector_check', 'done', `${known} known / ${newC} new`);
    }
    if (status === 'error') setStep('vector_check', 'error', data || 'Failed');
  }

  else if (step === 'llm_analysis') {
    if (status === 'running') {
      const msg = (data && data.message) ? data.message : (typeof data === 'string' ? data : 'AI analysis in progress…');
      setStep('llm_analysis', 'running', msg);
      showFeed(msg);
    }
    if (status === 'done') {
      setStep('llm_analysis', 'done', 'Analysis complete');
      hideFeed();
    }
    if (status === 'error') {
      setStep('llm_analysis', 'error', data || 'Failed');
      hideFeed();
    }
  }

  // ── Q&A events (15 generic questions in batches of 3) ───────────────────
  else if (step === 'llm_qa') {
    const batch     = data && data.batch  ? data.batch  : 1;
    const total     = data && data.total  ? data.total  : 1;
    const qrange    = data && data.qrange ? data.qrange : '';
    const questions = data && data.questions ? data.questions : [];

    if (status === 'running') {
      setStep('llm_analysis', 'running', `Questions ${qrange} (batch ${batch}/${total})…`);
      setFeedSubtitle(`Batch ${batch}/${total} — Questions ${qrange}`);
      const qRows = questions.map(q => `
        <div class="af-qa-row af-qa-pending" id="af-qa-${esc(q.id)}">
          <span class="af-qa-id">${esc(q.id)}</span>
          <div class="af-qa-body">
            <div class="af-qa-q">${esc(q.text)}</div>
            <div class="af-qa-a af-qa-pending-a"><span class="spin">⟳</span> asking…</div>
          </div>
        </div>
      `).join('');
      feedAppend('qa-batch', batch, `
        <div class="af-batch-header">
          <span class="af-batch-num">Batch ${batch}/${total}</span>
          <span class="af-qa-range">${esc(qrange)}</span>
          <span class="af-spinner"><span class="spin">⟳</span></span>
        </div>
        <div class="af-qa-list">${qRows}</div>
      `);
    }

    if (status === 'done') {
      const answers   = data && data.answers   ? data.answers   : {};
      const qRows = questions.map(q => `
        <div class="af-qa-row af-qa-done">
          <span class="af-qa-id">${esc(q.id)}</span>
          <div class="af-qa-body">
            <div class="af-qa-q">${esc(q.text)}</div>
            <div class="af-qa-a">${esc(answers[q.id] || '—')}</div>
          </div>
        </div>
      `).join('');
      feedUpdate('qa-batch', batch, `
        <div class="af-batch-header af-batch-done">
          <span class="af-batch-num">✅ Batch ${batch}/${total}</span>
          <span class="af-qa-range">${esc(qrange)}</span>
        </div>
        <div class="af-qa-list">${qRows}</div>
      `);
    }

    if (status === 'error') {
      const msg = data && data.message ? data.message : 'Batch failed';
      feedUpdate('qa-batch', batch, `
        <div class="af-batch-header af-batch-error">
          <span class="af-batch-num">⚠️ Batch ${batch}/${total}</span>
          <span class="af-qa-range">${esc(qrange)}</span>
          <span class="af-err-msg">${esc(msg)}</span>
        </div>
      `);
    }
  }

  // ── Unique questions event (Q16 / Q17) ──────────────────────────────────
  else if (step === 'llm_unique_q') {
    if (status === 'running') {
      setStep('llm_analysis', 'running', 'Generating 2 unique incident-specific questions…');
      setFeedSubtitle('Unique insight questions');
      feedAppend('unique-q', 0, `
        <div class="af-batch-header af-uq-header">
          <span class="af-uq-badge">✨ UNIQUE</span>
          <span class="af-spinner"><span class="spin">⟳</span> Generating incident-specific questions…</span>
        </div>
      `);
    }
    if (status === 'done') {
      const uqs = data && data.unique_qa ? data.unique_qa : [];
      const uqRows = uqs.map(uq => `
        <div class="af-qa-row af-qa-unique">
          <span class="af-qa-id af-qa-id-unique">${esc(uq.id)}</span>
          <div class="af-qa-body">
            <div class="af-qa-q af-qa-q-unique">${esc(uq.question)}</div>
            <div class="af-qa-a">${esc(uq.answer || '—')}</div>
          </div>
        </div>
      `).join('');
      feedUpdate('unique-q', 0, `
        <div class="af-batch-header af-batch-done af-uq-header">
          <span class="af-uq-badge">✨ UNIQUE</span>
          <span>${uqs.length} incident-specific insights generated</span>
        </div>
        <div class="af-qa-list">${uqRows || '<span style="color:var(--faint);font-size:11px">No unique questions generated</span>'}</div>
      `);
    }
    if (status === 'error') {
      feedUpdate('unique-q', 0, `
        <div class="af-batch-header af-batch-error">
          <span class="af-uq-badge">✨ UNIQUE</span>
          <span class="af-err-msg">Failed to generate unique questions</span>
        </div>
      `);
    }
  }

  // ── Synthesis event ──────────────────────────────────────────────────────
  else if (step === 'llm_synthesis') {
    if (status === 'running') {
      setStep('llm_analysis', 'running', 'Assembling report from Q&A answers…');
      setFeedSubtitle('Building report');
      feedAppend('synthesis', 0, `
        <div class="af-synth">
          <span class="af-spinner"><span class="spin">⟳</span></span>
          Mapping Q&amp;A answers to report sections…
        </div>
      `);
    }
    if (status === 'done') {
      feedUpdate('synthesis', 0, `
        <div class="af-synth af-synth-done">
          ✅ Report assembled — ${data && data.message ? esc(data.message) : 'complete'}
        </div>
      `);
    }
  }

  else if (step === 'complete') {
    // ── Similar reports found — show panel instead of running AI ────────────
    if (data && data.status === 'similar_found') {
      _similarReports = data.similar_reports || [];
      setTimeout(() => showSimilarReportsPanel(_similarReports), 400);
      return;
    }
    const results = data && data.results ? data.results : (Array.isArray(data) ? data : []);
    _results = results;
    renderResults(results);
    renderSummary(results);
    populateSidebar(results);
    // Brief delay so summary animates before page transition
    setTimeout(() => showPage('results'), 800);
  }
}

// ── Analysis feed ─────────────────────────────────────────────────────────
// Keyed slots: each item has a type+id key so updates replace in-place.
const _feedSlots = {};  // key → DOM id

function showFeed(subtitle) {
  const feed = document.getElementById('analysis-feed');
  if (feed) { feed.style.display = 'block'; }
  setFeedSubtitle(subtitle || '');
  document.getElementById('af-items').innerHTML = '';
  Object.keys(_feedSlots).forEach(k => delete _feedSlots[k]);
}

function hideFeed() {
  const feed = document.getElementById('analysis-feed');
  if (feed) feed.style.display = 'none';
}

function setFeedSubtitle(text) {
  const el = document.getElementById('af-subtitle');
  if (el) el.textContent = text;
}

function _feedKey(type, id) { return `${type}-${id}`; }

function feedAppend(type, id, html) {
  const key   = _feedKey(type, id);
  const domId = 'af-slot-' + Object.keys(_feedSlots).length;
  _feedSlots[key] = domId;
  const items = document.getElementById('af-items');
  if (!items) return;
  const div = document.createElement('div');
  div.className = 'af-item';
  div.id = domId;
  div.innerHTML = html;
  items.appendChild(div);
  // Auto-scroll to bottom
  items.scrollTop = items.scrollHeight;
}

function feedUpdate(type, id, html) {
  const key = _feedKey(type, id);
  const domId = _feedSlots[key];
  if (!domId) { feedAppend(type, id, html); return; }
  const el = document.getElementById(domId);
  if (el) {
    el.innerHTML = html;
    const items = document.getElementById('af-items');
    if (items) items.scrollTop = items.scrollHeight;
  }
}

// ── Step rendering ────────────────────────────────────────────────────────
function resetStep(key) {
  const el = document.getElementById('step-' + key);
  if (!el) return;
  el.className = 'step';
  el.querySelector('.step-icon').innerHTML = '⬜';
  const meta = document.getElementById('meta-' + key);
  if (meta) meta.textContent = 'Waiting…';
}

function setStep(key, state, meta) {
  const el = document.getElementById('step-' + key);
  if (!el) return;
  el.className = 'step ' + state;
  const icon = el.querySelector('.step-icon');
  if (state === 'running') icon.innerHTML = '<span class="spin">🔄</span>';
  else if (state === 'done')  icon.textContent = '✅';
  else if (state === 'error') icon.textContent = '❌';
  const metaEl = document.getElementById('meta-' + key);
  if (metaEl && meta) metaEl.textContent = meta;
}

// ── Summary bar ───────────────────────────────────────────────────────────
function renderSummary(results) {
  const known  = results.filter(r => r.status === 'known').length;
  const newR   = results.filter(r => r.status === 'new').length;
  const failed = results.filter(r => r.status === 'fail' || r.status === 'error').length;

  document.getElementById('sum-logs').textContent      = _logCount ? Number(_logCount).toLocaleString() : '—';
  document.getElementById('sum-incidents').textContent = results.length;
  document.getElementById('sum-known').textContent     = known;
  document.getElementById('sum-new').textContent       = newR;
  document.getElementById('sum-failed').textContent    = failed;

  document.getElementById('summary-bar').classList.add('visible');
}

// ── Sidebar ───────────────────────────────────────────────────────────────
function populateSidebar(results) {
  const container = document.getElementById('sidebar-content');
  if (!results || results.length === 0) {
    container.innerHTML = '<p style="color:var(--faint);font-size:12px;padding:8px 0">No results to display.</p>';
    return;
  }

  const r      = results[0];  // single consolidated result
  const report = r.report || r;
  const pill   = report.log_pill || {};
  const topErrors = pill.top_errors || [];
  const chain  = report.causal_chain || [];
  const status = r.status || 'new';

  let html = '';

  // ── Stats ────────────────────────────────────────────────────────────────
  html += `<div class="sidebar-section">
    <div class="sidebar-section-title">Summary</div>
    <div class="sidebar-stat-grid">
      <div class="sidebar-stat"><div class="val">${_logCount ? Number(_logCount).toLocaleString() : '—'}</div><div class="key">Log Lines</div></div>
      <div class="sidebar-stat"><div class="val">${pill.unique_error_patterns || '—'}</div><div class="key">Patterns</div></div>
      <div class="sidebar-stat"><div class="val ${status === 'known' ? 'green' : ''}">${status === 'known' ? '1' : '0'}</div><div class="key">Known</div></div>
      <div class="sidebar-stat"><div class="val ${status === 'new' ? 'accent' : ''}">${status === 'new' ? '1' : '0'}</div><div class="key">New</div></div>
    </div>
  </div>`;

  // ── Severity badge ───────────────────────────────────────────────────────
  const sev = (report.severity || 'medium').toLowerCase();
  const sevClass = sev === 'high' ? 'rca-sev--high' : sev === 'low' ? 'rca-sev--low' : 'rca-sev--medium';
  html += `<div class="sidebar-section">
    <div class="sidebar-section-title">Severity</div>
    <span class="rca-sev-badge ${sevClass}" style="display:inline-block">${sev.toUpperCase()}</span>
  </div>`;

  // ── Top error types (deduplicated — log_pill has multiple rows per type) ───
  if (topErrors.length > 0) {
    // Aggregate by error type, summing counts
    const typeMap = new Map();
    topErrors.forEach(e => {
      const t = e.type || 'UNKNOWN';
      typeMap.set(t, (typeMap.get(t) || 0) + (e.count || 0));
    });
    // Sort descending by total count
    const aggErrors = Array.from(typeMap.entries())
      .sort((a, b) => b[1] - a[1]);

    html += `<div class="sidebar-section">
      <div class="sidebar-section-title">Error Types</div>`;
    aggErrors.slice(0, 8).forEach(([type, count]) => {
      const typeClass = 'rca-type-tag rca-type-' + type;
      html += `<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:5px">
        <span class="${typeClass}">${esc(type)}</span>
        <span style="font-size:11px;color:var(--dim)">${Number(count).toLocaleString()}</span>
      </div>`;
    });
    html += `</div>`;
  }

  // ── Causal chain ─────────────────────────────────────────────────────────
  const chainNodes = chain.filter(c => c && c.trim() !== '');
  if (chainNodes.length > 0) {
    html += `<div class="sidebar-section">
      <div class="sidebar-section-title">Causal Chain</div>
      <div class="rca-chain" style="flex-direction:column;align-items:flex-start;gap:4px">`;
    chainNodes.forEach((node, i) => {
      if (i > 0) html += `<span style="color:var(--faint);padding-left:4px;font-size:12px">↓</span>`;
      html += `<span class="rca-chain-node" style="font-size:11px;padding:3px 10px">${esc(node)}</span>`;
    });
    html += `</div></div>`;
  }

  // ── Accept / Reject (only for new results) ───────────────────────────────
  if (status === 'new') {
    html += `<div class="sidebar-section" id="sidebar-actions">
      <div class="sidebar-section-title">Actions</div>
      <div class="result-actions" style="flex-direction:column;gap:8px">
        <button class="btn-accept" style="width:100%" onclick="acceptReport(0)">✓ Accept &amp; Store</button>
        <button class="btn-reject" style="width:100%" onclick="rejectReport(0, '${esc(r.incident && r.incident.incident_id || 'inc-0')}')">✗ Reject</button>
      </div>
    </div>`;
  } else if (status === 'known') {
    const prevRef = r.matched_report_id || report.matched_report_id || '';
    html += `<div class="sidebar-section">
      <div class="sidebar-section-title">Status</div>
      <div style="background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.2);border-radius:var(--radius);padding:8px 10px">
        <div style="color:var(--green);font-size:12px;font-weight:600">✅ Known Issue</div>
        ${prevRef ? `<div style="font-size:10px;color:var(--faint);margin-top:4px">Ref: ${esc(prevRef.substring(0,20))}</div>` : ''}
      </div>
    </div>`;
  }

  container.innerHTML = html;
}

// ── Results rendering (right panel) ──────────────────────────────────────
function renderResults(results) {
  const container = document.getElementById('results');
  if (!results || results.length === 0) {
    container.innerHTML = '<div class="empty">No incidents found in the selected time window.</div>';
    return;
  }
  container.innerHTML = results.map((r, idx) => renderCard(r, idx)).join('');
  // Auto-expand the first card
  const firstCard = document.getElementById('rcard-0');
  if (firstCard) firstCard.classList.add('expanded');
}

function renderCard(r, idx) {
  const status  = r.status || 'new';
  const report  = r.report || r;
  const inc     = r.incident || {};
  const incId   = inc.incident_id || report.incident_id || ('inc-' + idx);
  const pill    = report.log_pill || {};
  const errType = (pill.top_errors && pill.top_errors[0] && pill.top_errors[0].type)
                  || report.error_type || inc.error_type || 'UNKNOWN';
  const totalPatterns = pill.unique_error_patterns || inc.count || report.count || 0;

  const badgeLabel = status === 'known' ? 'KNOWN' : status === 'new' ? 'NEW' : 'FAIL';
  const prevRef    = r.matched_report_id || report.matched_report_id || '';

  const knownRef = (status === 'known' && prevRef)
    ? `<p class="known-ref">Previously stored as: <code>${esc(prevRef)}</code></p>` : '';

  // Use server-rendered Jinja2 HTML if present; otherwise minimal fallback
  const bodyHtml = report.html
    ? report.html
    : `<div class="result-section">
         <div class="result-section-title">Root Cause</div>
         <p class="result-text">${esc(report.root_cause || report.summary || 'No analysis available.')}</p>
       </div>`;

  return `
<div class="result-card ${status}" id="rcard-${idx}">
  <div class="result-header" onclick="toggleCard(${idx})">
    <span class="result-badge">${badgeLabel}</span>
    <span class="result-id">${esc(incId.substring(0,16))}</span>
    <span class="result-type">${esc(errType)}</span>
    ${totalPatterns ? `<span class="result-count">${totalPatterns} patterns</span>` : ''}
    <span class="result-chevron">▼</span>
  </div>
  <div class="result-body">
    ${bodyHtml}
    ${knownRef}
  </div>
</div>`;
}

function toggleCard(idx) {
  const el = document.getElementById('rcard-' + idx);
  if (el) el.classList.toggle('expanded');
}

// ── Accept / Reject ───────────────────────────────────────────────────────
async function acceptReport(idx) {
  const r = _results[idx];
  if (!r) return;

  // Update sidebar button
  const sidebarBtn = document.querySelector('#sidebar-actions .btn-accept');
  if (sidebarBtn) { sidebarBtn.disabled = true; sidebarBtn.textContent = '⏳ Storing…'; }

  try {
    const resp = await fetch('/api/rca/accept', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        report:    r.report || r,
        embedding: r.embedding || [],
        app_id:    document.getElementById('app-select').value,
      }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    // Update sidebar to show stored
    const actionsEl = document.getElementById('sidebar-actions');
    if (actionsEl) {
      actionsEl.innerHTML = `<div class="sidebar-section-title">Actions</div>
        <button class="btn-stored" disabled style="width:100%">✅ Stored</button>`;
    }
    _results[idx].status = 'stored';
  } catch (e) {
    if (sidebarBtn) { sidebarBtn.disabled = false; sidebarBtn.textContent = '✓ Accept & Store'; }
    alert('Failed to store: ' + e.message);
  }
}

async function rejectReport(idx, incidentId) {
  const sidebarBtn = document.querySelector('#sidebar-actions .btn-reject');
  try {
    await fetch('/api/rca/reject', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ incident_id: incidentId }),
    });
  } catch { /* fire-and-forget */ }
  if (sidebarBtn) {
    sidebarBtn.textContent = 'Rejected';
    sidebarBtn.classList.add('rejected');
    sidebarBtn.disabled = true;
  }
  const acceptBtn = document.querySelector('#sidebar-actions .btn-accept');
  if (acceptBtn) acceptBtn.disabled = true;
}

// ── Utilities ─────────────────────────────────────────────────────────────
function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

// ── Similar Reports Panel ─────────────────────────────────────────────────
function showSimilarReportsPanel(hits) {
  const appName = (() => {
    const sel = document.getElementById('app-select');
    const opt = sel && sel.selectedOptions[0];
    return (opt && opt.textContent.trim()) || document.getElementById('app-select').value || 'Unknown App';
  })();

  const cards = (hits || []).map((hit, i) => {
    const score    = hit.similarity_pct != null ? hit.similarity_pct : Math.round((hit.score || 0) * 100);
    const scoreCls = score >= 80 ? 'score-high' : score >= 65 ? 'score-med' : 'score-low';
    const sev      = (hit.severity || 'medium').toLowerCase();
    const sevCls   = sev === 'critical' ? 'rca-sev--critical' : sev === 'high' ? 'rca-sev--high' : sev === 'low' ? 'rca-sev--low' : 'rca-sev--medium';
    const date     = hit.created_at ? new Date(hit.created_at).toLocaleString() : '—';
    const summary  = hit.summary || hit.error_type || 'No summary available';
    const hitAppId = hit.app_id || '';
    // Look up app display name from the loaded apps list
    const appEntry = (_apps || []).find(a => a.app_id === hitAppId);
    const hitApp   = (appEntry && (appEntry.app_name || appEntry.app_id)) || hitAppId || appName;

    return `
<div class="similar-card" id="scard-${i}">
  <div class="similar-card-header">
    <span class="rca-sev-badge ${sevCls}">${sev.toUpperCase()}</span>
    <span class="score-pill ${scoreCls}">${score}% match</span>
    <span class="similar-card-date">${esc(date)}</span>
  </div>
  <div class="similar-card-title">${esc(hit.error_type || 'UNKNOWN')} — ${esc(hitApp)}</div>
  <div class="similar-card-summary">${esc(summary)}</div>
  <div class="similar-card-actions">
    <button class="btn-view-report" onclick="viewSimilarReport(${i})">📄 View Full Report</button>
  </div>
</div>`;
  }).join('');

  const panel = `
<div class="similar-panel" id="similar-panel">
  <div class="similar-panel-header">
    <div class="similar-panel-title">⚡ Similar Reports Found</div>
    <div class="similar-panel-subtitle">Review these ${hits.length} stored report${hits.length !== 1 ? 's' : ''} before generating a new AI analysis</div>
  </div>
  <div class="similar-panel-actions">
    <button class="btn-new-analysis" onclick="runRCA(true)">🤖 Run New AI Analysis</button>
  </div>
  <div class="similar-cards-list" id="similar-cards-list">
    ${cards || '<div style="color:var(--faint);padding:16px">No similar reports to display.</div>'}
  </div>
</div>`;

  document.getElementById('results').innerHTML = panel;
  document.getElementById('sidebar-content').innerHTML = `
<div class="sidebar-section">
  <div class="sidebar-section-title">Similar Reports</div>
  <div style="font-size:12px;color:var(--dim);line-height:1.5">
    Found <strong>${hits.length}</strong> stored report${hits.length !== 1 ? 's' : ''} with ≥60% similarity.<br><br>
    Review them below, or click <em>Run New AI Analysis</em> to generate a fresh report.
  </div>
</div>`;
  document.getElementById('summary-bar').classList.remove('visible');
  showPage('results');
}

/**
 * Build a full rich report body from stored report JSON fields.
 * Used as fallback when report.html is absent (reports stored before the
 * html-preserve fix). Mirrors all 13 sections of the server-rendered report.
 */
function _buildReportBodyFromFields(report) {
  const sev     = (report.severity || 'medium').toLowerCase();
  const sevCls  = sev === 'critical' ? 'rca-sev--critical' : sev === 'high' ? 'rca-sev--high' : sev === 'low' ? 'rca-sev--low' : 'rca-sev--medium';

  const section = (title, content) =>
    content ? `<div class="result-section"><div class="result-section-title">${title}</div>${content}</div>` : '';

  const para  = (t) => t ? `<p class="result-text">${esc(t)}</p>` : '';
  const steps = (arr) => Array.isArray(arr) && arr.length
    ? `<ol class="result-steps">${arr.map(s => `<li>${esc(s)}</li>`).join('')}</ol>` : '';
  const bullets = (arr) => Array.isArray(arr) && arr.length
    ? `<ul class="result-steps">${arr.map(s => `<li>${esc(s)}</li>`).join('')}</ul>` : '';
  const badge = `<span class="rca-sev-badge ${sevCls}" style="margin-bottom:8px;display:inline-block">${sev.toUpperCase()}</span>`;

  // ── 1. Executive Summary ──────────────────────────────────────────────────
  let html = section('Executive Summary',
    `${badge}${para(report.summary || report.problem_statement)}`
  );

  // ── 2. Problem Statement ─────────────────────────────────────────────────
  if (report.problem_statement && report.problem_statement !== report.summary) {
    html += section('Problem Statement', para(report.problem_statement));
  }

  // ── 3. Blast Radius ──────────────────────────────────────────────────────
  html += section('Blast Radius', para(report.blast_radius));

  // ── 4. Root Cause Analysis ────────────────────────────────────────────────
  html += section('Root Cause Analysis', para(report.root_cause));

  // ── 5. Contributing Factors ──────────────────────────────────────────────
  html += section('Contributing Factors', bullets(report.contributing_factors));

  // ── 6. Timeline of Events ────────────────────────────────────────────────
  html += section('Timeline of Events', steps(report.timeline));

  // ── 7. Causal Chain ──────────────────────────────────────────────────────
  if (Array.isArray(report.causal_chain) && report.causal_chain.length) {
    const nodes = report.causal_chain
      .map(n => `<span class="rca-chain-node">${esc(n)}</span>`)
      .join('<span style="color:var(--faint);padding:0 4px">→</span>');
    html += section('Causal Chain', `<div class="rca-chain" style="flex-wrap:wrap">${nodes}</div>`);
  }

  // ── 8. Recommended Fix Steps ─────────────────────────────────────────────
  html += section('Recommended Fix Steps', steps(report.fix_steps));

  // ── 9. Long-term Prevention ──────────────────────────────────────────────
  html += section('Long-term Prevention', steps(report.long_term_fixes));

  // ── 10. Verification Plan ────────────────────────────────────────────────
  html += section('Verification Plan', steps(report.verification_steps));

  // ── 11. Unique Insights (Q16/Q17) ────────────────────────────────────────
  const uqs = report.unique_qa || [];
  if (uqs.length) {
    const uqRows = uqs.map(uq => `
      <div style="margin-bottom:12px">
        <div style="font-size:12px;font-weight:600;color:var(--accent);margin-bottom:3px">✨ ${esc(uq.question)}</div>
        <div style="font-size:13px;color:var(--text);line-height:1.5">${esc(uq.answer || '—')}</div>
      </div>`).join('');
    html += section('Unique Insights', uqRows);
  }

  // ── 12. Error Types ──────────────────────────────────────────────────────
  const errTypes = report.error_types || (report.error_type ? [report.error_type] : []);
  if (errTypes.length) {
    const tags = errTypes.map(t => `<span class="rca-type-tag rca-type-${t}">${esc(t)}</span>`).join(' ');
    html += section('Error Types', `<div style="display:flex;flex-wrap:wrap;gap:6px">${tags}</div>`);
  }

  // ── 13. Full Q&A Reference (collapsible) ─────────────────────────────────
  const allQA = report.all_qa || [];
  if (allQA.length) {
    const qaRows = allQA.map(q =>
      q.answer ? `<div style="margin-bottom:10px">
        <div style="font-size:11px;font-weight:700;color:var(--dim);margin-bottom:2px">${esc(q.id)} — ${esc(q.question)}</div>
        <div style="font-size:12px;color:var(--text);line-height:1.5">${esc(q.answer)}</div>
      </div>` : ''
    ).join('');
    if (qaRows) {
      html += `<div class="result-section">
        <details>
          <summary class="result-section-title" style="cursor:pointer;user-select:none">Full Q&amp;A Reference (${allQA.length} questions)</summary>
          <div style="margin-top:10px">${qaRows}</div>
        </details>
      </div>`;
    }
  }

  return html || para('No report data available.');
}

function viewSimilarReport(idx) {
  const hit = _similarReports[idx];
  if (!hit) return;

  const report   = hit.report || {};
  // Prefer server-rendered HTML (stored since the html-preserve fix).
  // Fall back to building a full rich view from all stored report fields.
  const bodyHtml = report.html || _buildReportBodyFromFields(report);

  const score    = hit.similarity_pct != null ? hit.similarity_pct : Math.round((hit.score || 0) * 100);
  const sev      = (hit.severity || report.severity || 'medium').toLowerCase();
  const sevCls   = sev === 'critical' ? 'rca-sev--critical' : sev === 'high' ? 'rca-sev--high' : sev === 'low' ? 'rca-sev--low' : 'rca-sev--medium';
  const date     = hit.created_at ? new Date(hit.created_at).toLocaleString() : '—';
  const scoreCls = score >= 80 ? 'score-high' : score >= 65 ? 'score-med' : 'score-low';
  const appEntry = (_apps || []).find(a => a.app_id === (hit.app_id || ''));
  const appName  = (appEntry && (appEntry.app_name || appEntry.app_id)) || hit.app_id || '';

  const detailHtml = `
<div class="similar-detail-view">
  <div class="similar-detail-nav">
    <button class="btn-back-similar" onclick="backToSimilarReports()">← Back to Similar Reports</button>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <span class="rca-sev-badge ${sevCls}">${sev.toUpperCase()}</span>
      <span class="score-pill ${scoreCls}">${score}% match</span>
      ${appName ? `<span style="font-size:12px;color:var(--dim)">${esc(appName)}</span>` : ''}
      <span style="font-size:11px;color:var(--faint)">${esc(date)}</span>
    </div>
  </div>
  <div class="similar-detail-body">
    ${bodyHtml}
  </div>
  <div style="margin-top:16px;padding:0 4px">
    <button class="btn-new-analysis" onclick="runRCA(true)">🤖 Run New AI Analysis</button>
  </div>
</div>`;

  document.getElementById('results').innerHTML = detailHtml;
}

function backToSimilarReports() {
  showSimilarReportsPanel(_similarReports);
}

// ── Init ──────────────────────────────────────────────────────────────────
checkHealth();
setInterval(checkHealth, 30000);
loadApps();

pollSplunkStats();
setInterval(pollSplunkStats, 10000);

sliderLabel.textContent = fmtMinutes(slider.value);
