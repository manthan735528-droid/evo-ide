/* ============================================================
   Evo IDE V6.1 — app.js  (frontend controller)
   ============================================================
   Vanilla JS, no build step, no dependencies. Wires the DOM in
   index.html to the backend endpoints exposed by core/server_handler.py
   and consumes the NDJSON event stream produced by
   core/stream_handler.py::StreamSession.

   This file was missing from static/, which is why the browser logged:
     - GET /app.js 404 (Not Found)
     - Refused to execute script ... MIME type ('application/json')
   Dropping this file into <project>/static/app.js fixes both.
   ============================================================ */
'use strict';

/* ── tiny DOM helpers ──────────────────────────────────────── */
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const el = (id) => document.getElementById(id);

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/* ── HTTP helpers ──────────────────────────────────────────── */
async function apiGet(path) {
  const r = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}
async function apiPost(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}



/* ── Thinking Bubble Helpers ─────────────────────────────── */
function ensureThinkingBubble() {
  let bubble = el('thinking-bubble');
  if (!bubble) {
    bubble = document.createElement('div');
    bubble.id = 'thinking-bubble';
    bubble.className = 'msg msg--think';
    bubble.innerHTML = '<div class="think-head"><svg class="think-icon"><use href="#i-brain"/></svg><span class="think-dot"></span><span class="think-title">THINKING</span><span class="think-time" id="think-time">0.0s</span><canvas class="think-wave" id="think-wave" width="120" height="24"></canvas><svg class="think-toggle"><use href="#i-chevron"/></svg></div><div class="think-body"><code id="think-code"></code></div>';
    el('chat').appendChild(bubble);
    el('chat').scrollTop = el('chat').scrollHeight;
    bubble.querySelector('.think-head').addEventListener('click', function() {
      bubble.classList.toggle('is-collapsed');
    });
    startThinkWave();
    bubble._startTime = Date.now();
    bubble._timer = setInterval(function() {
      var t = ((Date.now() - bubble._startTime) / 1000).toFixed(1);
      var timeEl = el('think-time');
      if (timeEl) timeEl.textContent = t + 's';
    }, 100);
  }
  return bubble;
}
function removeThinkingBubble() {
  var bubble = el('thinking-bubble');
  if (bubble) {
    if (bubble._timer) clearInterval(bubble._timer);
    stopThinkWave();
    // Keep bubble in chat — just stop animation and mark complete
    var title = bubble.querySelector('.think-title');
    if (title) {
      var timeEl = bubble.querySelector('.think-time');
      var elapsed = timeEl ? timeEl.textContent : '';
      title.textContent = 'THOUGHT' + (elapsed ? ' · ' + elapsed : '');
    }
    bubble.classList.add('is-final');
    // Strip ids so next turn never resurrects this stale bubble
    bubble.removeAttribute('id');
    bubble.querySelectorAll('[id]').forEach(function(n) { n.removeAttribute('id'); });
  }
}
var _waveRaf = null, _waveCtx = null, _waveOffset = 0;
function startThinkWave() {
  var c = el('think-wave');
  if (!c || !c.getContext) return;
  _waveCtx = c.getContext('2d');
  var dpr = window.devicePixelRatio || 1;
  c.width = 120 * dpr; c.height = 24 * dpr;
  _waveCtx.scale(dpr, dpr);
  function draw() {
    if (!_waveCtx) return;
    var ctx = _waveCtx, w = 120, h = 24;
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = '#a78bfa';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    for (var x = 0; x < w; x++) {
      var y = h/2 + Math.sin((x + _waveOffset) * 0.12) * 6 + Math.sin((x + _waveOffset*1.5) * 0.08) * 3;
      x ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke();
    _waveOffset += 1.2;
    _waveRaf = requestAnimationFrame(draw);
  }
  draw();
}
function stopThinkWave() {
  if (_waveRaf) { cancelAnimationFrame(_waveRaf); _waveRaf = null; }
  _waveCtx = null;
}

/* ── Code Stream Panel Helpers ───────────────────────────── */
function ensureCodePanel(file) {
  var panel = el('code-stream-panel');
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'code-stream-panel';
    panel.className = 'msg msg--code';
    panel.innerHTML = '<div class="code-head"><span class="code-dot"></span><span class="code-title" id="code-panel-file">' + (file || 'code') + '</span><span class="code-meta" id="code-panel-meta">0 lines</span><button class="code-copy" id="code-panel-copy" title="Copy to clipboard">Copy</button></div><div class="code-scroll"><div class="code-gutter" id="code-panel-gutter"></div><pre class="code-body"><code id="code-panel-body"></code></pre></div>';
    el('chat').appendChild(panel);
    el('chat').scrollTop = el('chat').scrollHeight;
    var copyBtn = panel.querySelector('#code-panel-copy');
    var bodyEl = panel.querySelector('#code-panel-body');
    copyBtn.addEventListener('click', function() {
      navigator.clipboard.writeText(bodyEl.textContent).then(function() {
        var old = copyBtn.textContent;
        copyBtn.textContent = 'Copied!';
        copyBtn.classList.add('is-copied');
        setTimeout(function() { copyBtn.textContent = old; copyBtn.classList.remove('is-copied'); }, 1500);
      });
    });
  }
  return panel;
}
function removeCodePanel() {
  var panel = el('code-stream-panel');
  if (panel) {
    // Keep panel in chat — finalize it
    var dot = panel.querySelector('.code-dot');
    if (dot) {
      dot.style.animation = 'none';
      dot.style.background = 'var(--primary-2)';
      dot.style.boxShadow = '0 0 6px var(--primary-glow)';
    }
    var copyBtn = panel.querySelector('.code-copy');
    if (copyBtn) copyBtn.style.display = 'inline-flex';
    panel.classList.add('is-final');
    // Strip ids so next turn never resurrects this stale panel
    panel.removeAttribute('id');
    panel.querySelectorAll('[id]').forEach(function(n) { n.removeAttribute('id'); });
  }
}
function updateCodePanelMeta() {
  var body = el('code-panel-body');
  var meta = el('code-panel-meta');
  var gutter = el('code-panel-gutter');
  if (body && meta) {
    var lines = body.textContent.split('\n').length;
    var chars = body.textContent.length;
    meta.textContent = lines + ' lines · ' + chars + ' chars';
  }
  if (gutter && body) {
    var lines = body.textContent.split('\n').length;
    var out = [];
    for (var i = 1; i <= lines; i++) out.push(i);
    gutter.innerHTML = out.map(function(n) { return '<span>' + n + '</span>'; }).join('\n');
  }
}

/* ── Telemetry Toggle ───────────────────────────────────── */
function toggleTelemetry() {
  var tel = el('telemetry');
  var btn = el('tel-toggle');
  var collapsed = tel.classList.toggle('is-collapsed');
  if (btn) btn.textContent = collapsed ? '▶' : '▼';
}

/* ── toasts ────────────────────────────────────────────────── */
function toast(msg, kind = 'info', ms = 3200) {
  const wrap = el('toasts');
  if (!wrap) return;
  // stylesheet only defines toast--error and toast--warn; everything else is default.
  const mod = kind === 'error' ? ' toast--error' : kind === 'warn' ? ' toast--warn' : '';
  const titles = { error: 'Error', warn: 'Warning', ok: 'Done', info: 'Info' };
  const t = document.createElement('div');
  t.className = `toast${mod}`;
  t.innerHTML = `<div class="toast__title">${titles[kind] || 'Info'}</div><div class="toast__body"></div>`;
  t.querySelector('.toast__body').textContent = msg;
  wrap.appendChild(t);
  setTimeout(() => { t.classList.add('is-hiding'); setTimeout(() => t.remove(), 220); }, ms);
}

/* ============================================================
   LLM output cleaning
   ------------------------------------------------------------
   DeepSeek-R1 content tokens leak <think> fragments and often
   echo whole files back as ```lang fenced blocks. The real code
   already streams to the canvas + editor, so in the chat answer
   we strip reasoning and collapse fenced code into compact chips,
   then render light markdown. Works incrementally while streaming
   (an unclosed trailing fence renders as "writing code…").
   ============================================================ */
function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}

function hexA(hex, a) {
  hex = (hex || '').trim().replace('#', '');
  if (hex.length === 3) hex = hex.split('').map((x) => x + x).join('');
  if (hex.length < 6) return `rgba(61,220,151,${a})`;
  const n = parseInt(hex.slice(0, 6), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function stripReasoning(raw) {
  // drop closed reasoning blocks (<think> and <thinking>), then any trailing unclosed one still streaming
  return raw
    .replace(/<think>[\s\S]*?<\/think>/gi, '')
    .replace(/<thinking>[\s\S]*?<\/thinking>/gi, '')
    .replace(/<\/?think>/gi, '')
    .replace(/<\/?thinking>/gi, '')
    .replace(/<think>[\s\S]*$/i, '')
    .replace(/<thinking>[\s\S]*$/i, '');
}

function splitSegments(text) {
  // → [{type:'text',content} | {type:'code',lang,lines,closed}]
  const segs = [];
  const re = /```([^\n`]*)\n?([\s\S]*?)```/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) segs.push({ type: 'text', content: text.slice(last, m.index) });
    segs.push({ type: 'code', lang: (m[1] || '').trim(), content: m[2] || '', lines: (m[2] || '').split('\n').length, closed: true });
    last = re.lastIndex;
  }
  const tail = text.slice(last);
  const open = tail.indexOf('```');
  if (open >= 0) {
    if (open > 0) segs.push({ type: 'text', content: tail.slice(0, open) });
    const rest = tail.slice(open + 3);
    const nl = rest.indexOf('\n');
    const lang = (nl >= 0 ? rest.slice(0, nl) : rest).trim();
    const codeContent = nl >= 0 ? rest.slice(nl + 1) : '';
    segs.push({ type: 'code', lang: lang, content: codeContent, lines: 0, closed: false });
  } else if (tail) {
    segs.push({ type: 'text', content: tail });
  }
  return segs;
}

function mdInline(s) {
  return escapeHtml(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function mdBlock(s) {
  const out = [];
  let inList = false;
  const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
  for (const ln of s.split('\n')) {
    if (/^#{1,6}\s/.test(ln)) { closeList(); out.push(`<h4 class="ca-h">${mdInline(ln.replace(/^#+\s/, ''))}</h4>`); continue; }
    if (/^\s*[-*]\s+/.test(ln)) { if (!inList) { out.push('<ul class="ca-ul">'); inList = true; } out.push(`<li>${mdInline(ln.replace(/^\s*[-*]\s+/, ''))}</li>`); continue; }
    if (!ln.trim()) { closeList(); continue; }
    closeList();
    out.push(`<p class="ca-p">${mdInline(ln)}</p>`);
  }
  closeList();
  return out.join('');
}

function renderAnswer(raw) {
  const segs = splitSegments(stripReasoning(raw));
  let html = '';
  for (const seg of segs) {
    if (seg.type === 'text') {
      const t = seg.content.replace(/\n{3,}/g, '\n\n');
      if (t.trim()) html += mdBlock(t);
    } else if (seg.closed) {
      const langLabel = escapeHtml(seg.lang || 'text');
      const lines = seg.content.split('\n');
      const lineCount = lines.length;
      const highlighted = seg.lang === 'python' || seg.lang === 'py' ? highlightPython(seg.content) : escapeHtml(seg.content);
      const lineNumbers = lines.map(function(_, i) { return '<span>' + (i + 1) + '</span>'; }).join('\n');
      const uniqueId = 'cc-' + Math.random().toString(36).slice(2, 9);
      html += '<div class="ca-codecard"><div class="ca-codecard__head"><span class="ca-codecard__lang">' + langLabel + '</span><span class="ca-codecard__meta">' + lineCount + ' lines</span><button class="ca-codecard__copy" onclick="navigator.clipboard.writeText(this.closest(\'.ca-codecard\').querySelector(\'.ca-codecard__code\').textContent).then(()=>{this.textContent=\'Copied!\';this.classList.add(\'is-copied\');setTimeout(()=>{this.textContent=\'Copy\';this.classList.remove(\'is-copied\');},1500)})">Copy</button></div><div class="ca-codecard__body"><div class="ca-codecard__gutter">' + lineNumbers + '</div><pre class="ca-pre"><code class="ca-precode' + (seg.lang ? ' language-' + escapeHtml(seg.lang) : '') + ' ca-codecard__code" id="' + uniqueId + '">' + highlighted + '</code></pre></div></div>';
    } else {
      html += `<div class="ca-code ca-code--live"><span class="ca-code__dot"></span>writing code${seg.lang ? ` · ${escapeHtml(seg.lang)}` : ''}…</div>`;
    }
  }
  return html || '<span class="ca-cursor" aria-hidden="true">▍</span>';
}

// Streaming can deliver many 'token' events per animation frame — re-parsing
// and re-painting the full answer on every single one causes visible jank,
// worst on long code responses. Coalesce to at most one render per frame.
function scheduleAnswerRender(assistant, answer) {
  assistant._pendingAnswer = answer;
  if (assistant._rafId) return;
  assistant._rafId = requestAnimationFrame(() => {
    assistant._rafId = null;
    const chat = el('chat');
    const stick = chat.scrollTop + chat.clientHeight >= chat.scrollHeight - 24;
    const target = assistant._body || assistant;
    target.innerHTML = renderAnswer(assistant._pendingAnswer);
    if (stick) chat.scrollTop = chat.scrollHeight;
  });
}

function cancelAnswerRender(assistant) {
  if (assistant._rafId) {
    cancelAnimationFrame(assistant._rafId);
    assistant._rafId = null;
  }
}

/* ── global state ──────────────────────────────────────────── */
const S = {
  files: [],
  openTabs: [],          // [{path, content, dirty}]
  active: null,          // active tab path
  follow: true,
  thinking: true,
  autoApply: true,
  autoRun: false,
  runMode: 'system',
  streamId: null,
  reader: null,
  streaming: false,
  tpsHistory: [],
  tps: [],               // sparkline ring buffer
  sprintEnabled: true,
  pendingDiff: null,     // {tx_id, files}
  attachments: [],       // paths explicitly attached to the next chat turn
};

/* ============================================================
   Editor
   ============================================================ */
const codeEl   = () => el('code');
const gutterEl = () => el('gutter');

function renderGutter() {
  const code = codeEl().value;
  const lines = code.split('\n').length;
  let out = '';
  for (let i = 1; i <= lines; i++) out += '<div>' + i + '</div>';
  gutterEl().innerHTML = out;
}


function updateCursor() {
  const ta = codeEl();
  const upto = ta.value.slice(0, ta.selectionStart);
  const line = upto.split('\n').length;
  const col = upto.length - upto.lastIndexOf('\n');
  el('status-pos').textContent = `Ln ${line}, Col ${col}`;
}

function currentCursorLine() {
  const ta = codeEl();
  return ta.value.slice(0, ta.selectionStart).split('\n').length - 1;
}

/* ============================================================
   Tabs
   ============================================================ */
function findTab(path) { return S.openTabs.find(t => t.path === path); }

function renderTabs() {
  const tabs = el('tabs');
  tabs.innerHTML = '';
  if (!S.openTabs.length) {
    const empty = document.createElement('span');
    empty.className = 'tabs__empty';
    empty.textContent = 'No file open';
    tabs.appendChild(empty);
    return;
  }
  for (const tab of S.openTabs) {
    const b = document.createElement('button');
    b.className = 'tab' + (tab.path === S.active ? ' is-active' : '') + (tab.dirty ? ' is-dirty' : '');
    b.setAttribute('role', 'tab');
    b.innerHTML = `<span class="tab__name">${tab.path}</span>`;
    const close = document.createElement('button');
    close.className = 'tab__close';
    close.textContent = '×';
    close.title = 'Close';
    close.addEventListener('click', (e) => { e.stopPropagation(); closeTab(tab.path); });
    b.appendChild(close);
    b.addEventListener('click', () => activateTab(tab.path));
    tabs.appendChild(b);
  }
}

function activateTab(path) {
  const tab = findTab(path);
  if (!tab) return;
  S.active = path;
  codeEl().value = tab.content;
  renderGutter();
  updateSyntaxHighlight();
  updateCursor();
  el('status-file').textContent = path;
  el('composer-target').textContent = path;
  renderTabs();
  renderTree();
  if (path.endsWith('.py')) validateNow();
  else clearProblems();
}

function closeTab(path) {
  const idx = S.openTabs.findIndex(t => t.path === path);
  if (idx < 0) return;
  S.openTabs.splice(idx, 1);
  if (S.active === path) {
    const next = S.openTabs[idx] || S.openTabs[idx - 1];
    if (next) activateTab(next.path);
    else { S.active = null; codeEl().value = ''; renderGutter(); el('status-file').textContent = 'no file'; renderTabs(); }
  } else {
    renderTabs();
  }
}

async function openFile(path) {
  let tab = findTab(path);
  if (!tab) {
    try {
      const data = await apiGet(`/api/file?name=${encodeURIComponent(path)}`);
      if (data.error) { toast(data.error, 'error'); return; }
      tab = { path, content: data.content ?? '', dirty: false };
      S.openTabs.push(tab);
      if (data.diagnostics) renderProblems(data.diagnostics);
    } catch (e) { toast(String(e), 'error'); return; }
  }
  activateTab(path);
}

/* ============================================================
   File tree
   ============================================================ */
async function loadFiles() {
  try {
    const data = await apiGet('/api/files');
    S.files = data.files || [];
  } catch { S.files = []; }
  renderTree();
}

function renderTree() {
  const tree = el('file-tree');
  const empty = el('tree-empty');
  tree.innerHTML = '';
  if (!S.files.length) { empty.hidden = false; return; }
  empty.hidden = true;
  for (const f of S.files) {
    const li = document.createElement('li');
    li.className = 'tree__item' + (f.path === S.active ? ' is-active' : '');
    li.setAttribute('role', 'treeitem');
    const ext = (f.path.split('.').pop() || '').slice(0, 3);
    li.innerHTML =
      `<span class="tree__ext">${ext}</span>` +
      `<span class="tree__name">${f.path}</span>` +
      `<span class="tree__size">${fmtBytes(f.size || 0)}</span>`;
    const del = document.createElement('button');
    del.className = 'iconbtn';
    del.title = 'Delete';
    del.innerHTML = '<svg class="ico"><use href="#i-trash"/></svg>';
    del.addEventListener('click', (e) => { e.stopPropagation(); deleteFile(f.path); });
    li.appendChild(del);
    li.addEventListener('click', () => openFile(f.path));
    tree.appendChild(li);
  }
}

function flashTree(path) {
  const items = $$('.tree__item');
  for (const it of items) {
    if ($('.tree__name', it)?.textContent === path) {
      it.classList.add('is-touched');
      setTimeout(() => it.classList.remove('is-touched'), 900);
    }
  }
}

async function deleteFile(path) {
  if (!confirm(`Delete ${path}?`)) return;
  const r = await apiPost('/api/delete', { filename: path });
  if (r.success) { toast(`Deleted ${path}`, 'ok'); closeTab(path); await loadFiles(); }
  else toast(r.message || 'Delete failed', 'error');
}

/* ============================================================
   Chat attachments — pick files from the sandbox instead of
   typing/guessing a path. Selected files are sent full-content
   with the next chat turn (see sendPrompt).
   ============================================================ */
function renderAttachments() {
  const wrap = el('composer-attachments');
  wrap.innerHTML = '';
  if (!S.attachments.length) { wrap.hidden = true; return; }
  wrap.hidden = false;
  for (const path of S.attachments) {
    const chip = document.createElement('span');
    chip.className = 'chip chip--attachment';
    const ext = (path.split('.').pop() || '').slice(0, 3);
    chip.innerHTML = `<span class="tree__ext">${ext}</span><span>${path}</span>`;
    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'chip__remove';
    rm.title = 'Remove attachment';
    rm.textContent = '×';
    rm.addEventListener('click', () => toggleAttachment(path));
    chip.appendChild(rm);
    wrap.appendChild(chip);
  }
}

function toggleAttachment(path) {
  const i = S.attachments.indexOf(path);
  if (i >= 0) S.attachments.splice(i, 1);
  else S.attachments.push(path);
  renderAttachments();
  renderAttachMenu();
}

function renderAttachMenu() {
  const list = el('attach-menu-list');
  list.innerHTML = '';
  if (!S.files.length) {
    const empty = document.createElement('div');
    empty.className = 'attach-menu__empty';
    empty.textContent = 'No files in the sandbox yet';
    list.appendChild(empty);
    return;
  }
  for (const f of S.files) {
    const attached = S.attachments.includes(f.path);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'attach-menu__item' + (attached ? ' is-attached' : '');
    btn.innerHTML =
      `<span>${attached ? '✓ ' : ''}${f.path}</span>` +
      `<span class="muted">${fmtBytes(f.size || 0)}</span>`;
    btn.addEventListener('click', () => toggleAttachment(f.path));
    list.appendChild(btn);
  }
}

async function toggleAttachMenu() {
  const menu = el('attach-menu');
  const opening = menu.hidden;
  if (opening) {
    await loadFiles();       // always refresh — sandbox may have changed
    renderAttachMenu();
  }
  menu.hidden = !opening;
}

async function newFile() {
  const name = prompt('New file name:', 'untitled.py');
  if (!name) return;
  const r = await apiPost('/api/newfile', { filename: name });
  if (r.success) { await loadFiles(); openFile(r.path); }
  else toast(r.error || 'Could not create file', 'error');
}

/* ============================================================
   Autosave — edits are written to disk shortly after you stop
   typing, so nothing depends on remembering to hit save.
   ============================================================ */
let autosaveTimer = null;

function scheduleAutosave() {
  clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(() => { saveActive(true); }, 700);
}

function flashSynced() {
  const s = el('status-sync');
  if (!s) return;
  const t = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  s.textContent = `synced ${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
  s.classList.add('status--flash');
  clearTimeout(s._flashTimer);
  s._flashTimer = setTimeout(() => s.classList.remove('status--flash'), 900);
}

async function saveActive(silent = false) {
  clearTimeout(autosaveTimer);
  if (!S.active) return;
  const tab = findTab(S.active);
  if (!tab) return;
  tab.content = codeEl().value;
  const r = await apiPost('/api/save', { filename: tab.path, code: tab.content });
  if (r.success) {
    tab.dirty = false; renderTabs(); renderTree();
    if (silent) flashSynced(); else toast(`Saved ${tab.path}`, 'ok', 1500);
    if (tab.path.endsWith('.py')) validateNow();
    return true;
  }
  toast(r.detail || 'Save failed', 'error');
  return false;
}

/* ============================================================
   Validation / Problems
   ============================================================ */
function clearProblems() { el('problems-list').innerHTML = ''; setProblemCount(0, false); el('status-diag').textContent = '0 problems'; }
function setProblemCount(n, bad) {
  const c = el('problem-count');
  c.textContent = String(n);
  c.dataset.bad = bad ? '1' : '0';
}
function renderProblems(diags) {
  const list = el('problems-list');
  list.innerHTML = '';
  let errors = 0;
  for (const d of diags) {
    if (d.severity === 'error') errors++;
    const row = document.createElement('div');
    row.className = `problem problem--${d.severity}`;
    const sev = d.severity === 'error' ? '×' : d.severity === 'warning' ? '!' : 'i';
    row.innerHTML =
      `<span class="problem__sev">${sev}</span>` +
      `<span class="problem__loc">Ln ${d.line}</span>` +
      `<span class="problem__msg">${d.message}${d.suggestion ? `<small>${d.suggestion}</small>` : ''}</span>`;
    row.addEventListener('click', () => gotoLine(d.line));
    list.appendChild(row);
  }
  setProblemCount(diags.length, errors > 0);
  el('status-diag').textContent = `${diags.length} problem${diags.length === 1 ? '' : 's'}`;
}
function gotoLine(line) {
  const ta = codeEl();
  const lines = ta.value.split('\n');
  let pos = 0;
  for (let i = 0; i < line - 1 && i < lines.length; i++) pos += lines[i].length + 1;
  ta.focus();
  ta.setSelectionRange(pos, pos);
  updateCursor();
}
const validateNow = debounce(async () => {
  if (!S.active || !S.active.endsWith('.py')) return;
  try {
    const r = await apiPost('/api/validate', { filename: S.active, code: codeEl().value });
    renderProblems(r.diagnostics || []);
  } catch {}
}, 500);

/* ============================================================
   Run / Terminal / Stop
   ============================================================ */
async function runActive() {
  const target = S.active || 'main.py';
  if (S.active) await saveActive();
  termLine(`$ python3 ${target}`, 'cmd');
  switchPanel('terminal');
  const r = await apiPost('/api/run', { filename: target });
  if (r.stdout) termLine(r.stdout, 'ok');
  if (r.stderr) termLine(r.stderr, 'err');
  if (!r.auto_run && r.terminal_command) termLine(`(auto-run off) run manually: ${r.terminal_command}`, 'sys');
  if (r.stats) applySession(r.stats);
}

function termLine(text, kind = '') {
  const log = el('term-log');
  const div = document.createElement('div');
  div.className = 'term__line' + (kind ? ` line--${kind}` : '');
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}
function outLine(text, kind = '') {
  const log = el('output-log');
  const div = document.createElement('div');
  div.className = 'term__line' + (kind ? ` line--${kind}` : '');
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function runTerminal(cmd) {
  termLine(`$ ${cmd}`, 'cmd');
  const r = await apiPost('/api/terminal', { command: cmd });
  if (r.stdout) termLine(r.stdout, 'ok');
  if (r.stderr) termLine(r.stderr, 'err');
  if (!r.stdout && !r.stderr) termLine('(no output)', 'sys');
}

async function stopStream() {
  if (S.streamId) await apiPost('/api/stop', { stream_id: S.streamId });
  else await apiPost('/api/stop', {});
  toast('Stream stopped', 'warn', 1500);
}

/* ============================================================
   Chat + NDJSON streaming
   ============================================================ */
function setStreaming(on) {
  S.streaming = on;
  el('btn-stop').disabled = !on;
  el('btn-send').disabled = on;
  el('btn-run').disabled = on;
  el('btn-sprint').disabled = on || S.sprintEnabled === false;
  el('stream-dot').className = 'dot' + (on ? ' dot--live' : '');
  el('tel-state').textContent = on ? 'streaming' : 'idle';
}

function chatBubble(role, text) {
  const cls = role === 'assistant' ? 'ai' : role;
  const chat = el('chat');
  const b = document.createElement('div');
  b.className = `msg msg--${cls}`;
  const label = role === 'user' ? 'You' : role === 'assistant' ? 'Evo' : 'System';
  b.innerHTML = `<div class="msg__head">${escapeHtml(label)}</div><div class="msg__body"></div>`;
  const body = b.querySelector('.msg__body');
  if (text) body.textContent = text;
  chat.appendChild(b);
  chat.scrollTop = chat.scrollHeight;
  b._body = body;
  return b;
}

function showStreamOverlay(file) {
  el('stream-file').textContent = file;
  el('stream-code').textContent = '';
  el('stream-bytes').textContent = '0 B';
  el('stream-overlay').hidden = false;
}
function hideStreamOverlay() { el('stream-overlay').hidden = true; }

async function sendPrompt(sprint = false) {
  const promptEl = el('prompt');
  const text = promptEl.value.trim();
  if (!text || S.streaming) return;
  if (sprint && !S.sprintEnabled) {
    toast('Sprint is disabled in this deployment (cloud mode)', 'warn');
    return;
  }
  // Persist the active editor buffer before asking the agent to build context.
  // This guarantees the model sees the latest unsaved edits and prevents a
  // generated artifact from being based on stale on-disk content.
  if (S.active) {
    const saved = await saveActive();
    if (!saved) return;
  }

  const attachedNote = S.attachments.length ? `\n📎 ${S.attachments.join(', ')}` : '';
  chatBubble('user', text + attachedNote);
  promptEl.value = '';
  el('composer-tokens').textContent = '0 tok';

  // The previous turn's thinking bubble (if any) keeps its id so it stays
  // put — otherwise ensureThinkingBubble() below would find that stale,
  // already-finalized element and reuse it instead of creating a new one
  // for this turn, leaving this turn's reasoning invisible further down.
  const prevThink = el('thinking-bubble');
  if (prevThink) {
    prevThink.removeAttribute('id');
    prevThink.querySelectorAll('[id]').forEach((n) => n.removeAttribute('id'));
  }
  const prevCode = el('code-stream-panel');
  if (prevCode) {
    prevCode.removeAttribute('id');
    prevCode.querySelectorAll('[id]').forEach((n) => n.removeAttribute('id'));
  }

  const endpoint = sprint ? '/api/sprint_stream' : '/api/chat_stream';
  const body = {
    prompt: text,
    currentFile: S.active || 'main.py',
    cursorLine: currentCursorLine(),
    thinking: S.thinking,
    autoApply: S.autoApply,
    autoRun: S.autoRun,
    attachments: S.attachments,
  };
  S.attachments = [];
  renderAttachments();

  const assistant = chatBubble('assistant', '');
  const reasoningLog = el('reasoning-log');
  reasoningLog.textContent = '';
  setStreaming(true);

  try {
    const resp = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    S.streamId = resp.headers.get('X-Stream-Id');
    if (!resp.body) throw new Error('No response stream');

    const reader = resp.body.getReader();
    S.reader = reader;
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let answer = '';
    let streamFile = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        answer = handleEvent(ev, { assistant, reasoningLog, answer, streamFileRef: (f) => (streamFile = f), getStreamFile: () => streamFile });
      }
    }
  } catch (e) {
    outLine(String(e), 'err');
    toast(String(e), 'error');
  } finally {
    setStreaming(false);
    S.reader = null;
    S.streamId = null;
    hideStreamOverlay();
    await loadFiles();
  }
}

function handleEvent(ev, ctx) {
  const { assistant, reasoningLog } = ctx;
  let answer = ctx.answer;

  switch (ev.event) {
    case 'stream_start':
      if (ev.session) applySession(ev.session);
      if (!el('telemetry').classList.contains('is-collapsed')) toggleTelemetry();
      break;
    case 'status':
      el('tel-state').textContent = ev.text || 'working';
      break;
    case 'context':
      renderTpsMetrics(ev);
      break;
    case 'thinking_start':
      ensureThinkingBubble();
      break;
    case 'thinking': {
      var bubble = ensureThinkingBubble();
      var code = bubble.querySelector('#think-code');
      if (code) {
        var text = (ev.text || '').replace(/<\/?think>/gi, '').replace(/<\/?thinking>/gi, '');
        code.textContent += text;
      }
      el('chat').scrollTop = el('chat').scrollHeight;
      break;
    }
    case 'thinking_end':
      removeThinkingBubble();
      break;
    case 'token': {
      answer += ev.text || '';
      scheduleAnswerRender(assistant, answer);
      break;
    }
    case 'artifact_start':
      ctx.streamFileRef(ev.file);
      showStreamOverlay(ev.file);
      ensureCodePanel(ev.file);
      break;
    case 'artifact_delta': {
      var codeNode = el('stream-code');
      var clean = (ev.text || '').replace(/^\s*```[^\n]*\n?/, '').replace(/\n?```\s*$/, '');
      codeNode.textContent += clean;
      var overlay = codeNode.closest('.editor__streambody');
      if (overlay && S.follow) overlay.scrollTop = overlay.scrollHeight;
      var lines = codeNode.textContent ? codeNode.textContent.split('\n').length : 0;
      el('stream-bytes').textContent = lines + 'L · ' + fmtBytes(ev.end || codeNode.textContent.length);
      var cpBody = el('code-panel-body');
      if (cpBody) { cpBody.textContent += clean; updateCodePanelMeta(); }
      break;
    }
    case 'artifact_end': {
      var codeNode = el('stream-code');
      var lines = codeNode.textContent ? codeNode.textContent.split('\n').length : 0;
      el('stream-bytes').textContent = lines + 'L · ' + fmtBytes(ev.bytes || 0);
      updateCodePanelMeta();
      removeCodePanel();
      break;
    }
    case 'file_written':
      toast(`Wrote ${ev.file} (${ev.lines} lines)`, 'ok', 2000);
      flashTree(ev.file);
      if (ev.diagnostics && ev.file === S.active) renderProblems(ev.diagnostics);
      refreshWrittenFile(ev.file);
      break;
    case 'file_patched':
      toast(`Patched ${ev.file}:${ev.line}`, 'ok', 2000);
      flashTree(ev.file);
      refreshWrittenFile(ev.file);
      break;
    case 'rejected':
      outLine(ev.text || `Rejected ${ev.file}`, 'warn');
      break;
    case 'diff_preview':
      openDiffReview(ev);
      break;
    case 'diff_timeout':
      toast('Diff review timed out — changes discarded', 'warn');
      closeDiffReview(ev.tx_id);
      break;
    case 'file_rejected_by_user':
      outLine(`Rejected by you: ${ev.file}`, 'warn');
      flashTree(ev.file);
      break;
    case 'execution':
      outLine(`${ev.file}: ${ev.success ? 'OK' : 'FAILED'} (${(ev.duration || 0).toFixed?.(2) ?? ev.duration}s)`,
        ev.success ? 'ok' : 'err');
      switchPanel('output');
      break;
    case 'stdout':
      outLine(ev.text || '', 'ok');
      break;
    case 'stderr':
      outLine(ev.text || '', 'err');
      break;
    case 'terminal_hint':
      termLine(`hint: ${ev.text}`, 'sys');
      break;
    case 'usage':
      break;
    case 'telemetry':
      applyTelemetry(ev);
      break;
    case 'sprint_iteration':
      chatBubble('sys', `— sprint iteration ${ev.iteration} —`);
      break;
    case 'sprint_complete':
      toast(`Sprint complete (${ev.iterations} iterations)`, 'ok');
      break;
    case 'sprint_halted':
      toast(`Sprint halted: ${ev.reason}`, 'warn');
      break;
    case 'complete':
      // final pass: drop the live cursor / half-open fences now the answer is settled
      cancelAnswerRender(assistant);
      const targetComplete = assistant._body || assistant;
      targetComplete.innerHTML = renderAnswer(answer) || '<span class="ca-p ca-muted">(no message)</span>';
      break;
    case 'stream_end':
      if (ev.session) applySession(ev.session);
      cancelAnswerRender(assistant);
      const targetStreamEnd = assistant._body || assistant;
      targetStreamEnd.innerHTML = renderAnswer(answer) || '<span class="ca-p ca-muted">(no message)</span>';
      wireLine('stream_end · ' + fmtBytes(ev.artifact_bytes || 0) + ' artifacts');
      if (el('telemetry').classList.contains('is-collapsed')) toggleTelemetry();
      break;
    case 'error':
      outLine(ev.text || 'error', 'err');
      toast(ev.text || 'Stream error', 'error');
      break;
    case 'heartbeat':
      break;
    default:
      break;
  }

  if (typeof ev.seq === 'number') wireLine(`#${ev.seq} ${ev.event}`);
  return answer;
}

async function refreshWrittenFile(path) {
  const tab = findTab(path);
  try {
    const data = await apiGet(`/api/file?name=${encodeURIComponent(path)}`);
    if (data.error) return;
    if (tab) {
      tab.content = data.content ?? '';
      tab.dirty = false;
      if (S.active === path) { codeEl().value = tab.content; renderGutter(); }
    }
  } catch {}
}

/* ============================================================
   Telemetry / Inspector
   ============================================================ */
function renderTpsMetrics(report) {
  const list = el('budget-list');
  list.innerHTML = '';
  const order = ['system', 'active_file', 'workspace', 'rag', 'terminal', 'web', 'history', 'user_prompt'];
  let totalTokens = 0;
  for (const key of order) {
    if (!(key in report)) continue;
    const val = report[key] || 0;
    totalTokens += val;
    const row = document.createElement('div');
    row.className = 'budget__row';
    row.innerHTML =
      `<div class="budget__top"><b>${key}</b><span>${val} tok</span></div>`;
    list.appendChild(row);
  }
  const totalRow = document.createElement('div');
  totalRow.className = 'budget__row budget__row--total';
  totalRow.innerHTML =
    `<div class="budget__top"><b>TOTAL</b><span>${totalTokens} tok</span></div>`;
  list.appendChild(totalRow);
  const badge = el('rail-ctx-badge');
  if (report.total) { badge.hidden = false; badge.textContent = report.total; }
}

function applyTelemetry(ev) {
  const m = ev.metrics || {};
  const h = ev.health || {};
  el('tel-tps').textContent = (m.tps ?? 0).toFixed ? m.tps.toFixed(1) : m.tps || '0.0';
  el('tel-peak').textContent = Math.round(m.peak_tps || 0);
  el('tel-avg').textContent = Math.round(m.avg_tps || 0);
  el('tel-ttft').textContent = m.ttft_ms ? `${m.ttft_ms}ms` : '—';
  el('tel-tokens').textContent = m.tokens || 0;
  // TPS display uses sparkline instead of budget bar
  el('tel-think').textContent = m.reasoning_tokens || 0;
  el('tel-code').textContent = m.content_tokens || 0;
  el('tel-elapsed').textContent = `${Math.round((m.elapsed_ms || 0) / 1000)}s`;

  const barPct = Math.min(100, Math.round((m.tps || 0) / 50 * 100));
  const bar = el('bar-tokens');
  bar.style.width = `${barPct}%`;
  bar.className = 'tps-bar__fill' + (barPct > 80 ? ' tps-bar__fill--fast' : '');

  // TPS sparkline for token velocity display
  S.tps.push(m.tps || 0);
  if (S.tps.length > 120) S.tps.shift();
  drawSpark();

  // health gauge
  if (typeof h.score === 'number') drawGauge(h.score);
  setCheck('chk-syntax', h.syntax_bad === 0, h.syntax_bad ? `${h.syntax_bad} bad` : 'ok');
  setCheck('chk-fences', !!h.fences_balanced, h.fences_balanced ? 'yes' : 'no');
  const tr = ev.transport || {};
  el('chk-frames').textContent = tr.frames || 0;
  el('chk-loss').textContent = tr.dropped || 0;

  if (ev.artifacts) renderArtifacts(ev.artifacts);
}

function setCheck(id, ok, label) {
  const b = el(id);
  if (!b) return;
  b.textContent = label;
  const tick = b.parentElement.querySelector('.tick');
  if (tick) tick.dataset.state = ok ? 'ok' : 'bad';
}

function setupSpark() {
  const c = el('spark');
  if (!c) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = c.getBoundingClientRect();
  const cssW = Math.round(rect.width) || c.width || 340;
  const cssH = Math.round(rect.height) || c.height || 38;
  c.width = Math.round(cssW * dpr);
  c.height = Math.round(cssH * dpr);
  c._dpr = dpr; c._cssW = cssW; c._cssH = cssH;
  drawSpark();
}

function drawSpark() {
  const c = el('spark');
  if (!c || !c.getContext) return;
  const ctx = c.getContext('2d');
  const dpr = c._dpr || 1;
  const w = c._cssW || c.width;
  const h = c._cssH || c.height;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const css = getComputedStyle(document.documentElement);
  const primary = (css.getPropertyValue('--primary') || '#3ddc97').trim() || '#3ddc97';
  const lineCol = (css.getPropertyValue('--line') || '#232a34').trim() || '#232a34';

  // baseline
  ctx.strokeStyle = lineCol;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, h - 1);
  ctx.lineTo(w, h - 1);
  ctx.stroke();

  const data = S.tps;
  if (data.length < 2) return;
  const pad = 3;
  const max = Math.max(...data, 1);
  const X = (i) => (i / (data.length - 1)) * w;
  const Y = (v) => h - pad - (v / max) * (h - pad * 2);

  // area fill
  ctx.beginPath();
  ctx.moveTo(0, h);
  data.forEach((v, i) => ctx.lineTo(X(i), Y(v)));
  ctx.lineTo(w, h);
  ctx.closePath();
  ctx.fillStyle = hexA(primary, 0.14);
  ctx.fill();

  // line
  ctx.beginPath();
  data.forEach((v, i) => (i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v))));
  ctx.strokeStyle = primary;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.stroke();

  // live head dot
  const hx = X(data.length - 1);
  const hy = Y(data[data.length - 1]);
  ctx.beginPath();
  ctx.arc(hx, hy, 2.4, 0, Math.PI * 2);
  ctx.fillStyle = primary;
  ctx.fill();
}

function drawGauge(score) {
  el('tel-score').textContent = score;
  const arc = el('gauge-arc');
  if (!arc) return;
  const r = 30;
  const circ = 2 * Math.PI * r;
  arc.style.strokeDasharray = String(circ);
  arc.style.strokeDashoffset = String(circ * (1 - score / 100));
  arc.style.stroke = score >= 80 ? '#3ddc97' : score >= 50 ? '#f0a742' : '#f2555a';
}

function renderArtifacts(arts) {
  const ul = el('artifacts');
  ul.innerHTML = '';
  for (const a of arts) {
    const li = document.createElement('li');
    const state = a.syntax_ok === false ? 'bad' : a.closed ? 'ok' : 'live';
    li.className = `artifact artifact--${state}`;
    li.innerHTML =
      `<span class="artifact__name">${a.path}</span>` +
      `<span class="artifact__size">${a.lines}L · ${fmtBytes(a.bytes)}</span>` +
      `<span class="artifact__state"></span>`;
    ul.appendChild(li);
  }
}

function wireLine(text) {
  const log = el('wire-log');
  if (!log) return;
  const div = document.createElement('div');
  div.className = 'term__line line--sys';
  div.textContent = text;
  log.appendChild(div);
  while (log.children.length > 400) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

/* ============================================================
   Session snapshot → status bar / settings
   ============================================================ */
function applySession(s) {
  if (!s) return;
  el('status-chunks').textContent = `${s.context_chunks ?? 0} chunks`;
  el('status-tokens').textContent = `${s.tokens ?? 0} tok`;
  el('status-saved').textContent = `$${(s.money ?? 0).toFixed ? s.money.toFixed(2) : s.money} saved`;
  el('status-run').textContent = s.run_mode || 'system';
  if (s.model) { el('model-name').textContent = s.model; }
  if (s.num_ctx) el('chip-ctx').textContent = `ctx ${s.num_ctx} / ${s.max_output}`;
  if (typeof s.sprint_enabled === 'boolean') {
    S.sprintEnabled = s.sprint_enabled;
    const sprintBtn = el('btn-sprint');
    if (sprintBtn) {
      sprintBtn.disabled = S.streaming || !S.sprintEnabled;
      sprintBtn.title = S.sprintEnabled
        ? 'Autonomous multi-turn sprint'
        : 'Sprint disabled in cloud mode — set EVO_SPRINT_ENABLED=true to opt in';
      sprintBtn.classList.toggle('is-disabled-mode', !S.sprintEnabled);
    }
  }
  const kv = el('settings-kv');
  if (kv) {
    kv.innerHTML =
      `<dt>model</dt><dd>${s.model || '—'}</dd>` +
      `<dt>num_ctx</dt><dd>${s.num_ctx ?? '—'}</dd>` +
      `<dt>max_out</dt><dd>${s.max_output ?? '—'}</dd>` +
      
      `<dt>files</dt><dd>${s.files ?? 0}</dd>` +
      `<dt>runs</dt><dd>${s.runs ?? 0}</dd>` +
      `<dt>fixes</dt><dd>${s.fixes ?? 0}</dd>` +
      `<dt>python</dt><dd>${s.system_python || '—'}</dd>` +
      `<dt>deployment</dt><dd>${s.cloud_mode ? 'cloud' : 'local'}</dd>` +
      `<dt>diff review</dt><dd>${s.transactional_apply ? 'on' : 'off'}</dd>` +
      `<dt>sprint</dt><dd>${s.sprint_enabled ? 'enabled' : 'disabled'}</dd>`;
  }
}

/* ============================================================
   Health / models / templates / structure / web
   ============================================================ */
async function loadHealth() {
  try {
    const h = await apiGet('/api/health');
    const dot = el('model-dot');
    const online = h.ok;
    dot.className = 'dot ' + (online ? 'dot--ok' : 'dot--bad');
    el('model-name').textContent = h.ollama?.model || h.configured_model || 'unknown';
    el('health-dot').className = 'dot ' + (online ? 'dot--ok' : 'dot--bad');
    el('health-text').textContent = online
      ? 'Ollama online'
      : (h.ollama?.model_available === false ? 'model not pulled' : 'Ollama offline');
    if (h.session) applySession(h.session);
  } catch {
    el('model-dot').className = 'dot dot--bad';
    el('health-dot').className = 'dot dot--bad';
    el('health-text').textContent = 'server unreachable';
  }
}

async function loadModels() {
  try {
    const data = await apiGet('/api/models');
    const sel = el('model-select');
    sel.innerHTML = '';
    for (const m of data.models || []) {
      const o = document.createElement('option');
      o.value = m; o.textContent = m;
      if (m === data.active) o.selected = true;
      sel.appendChild(o);
    }
  } catch {}
}

async function loadTemplates() {
  try {
    const data = await apiGet('/api/templates');
    const sel = el('template-select');
    for (const [key, meta] of Object.entries(data.templates || {})) {
      const o = document.createElement('option');
      o.value = key; o.textContent = `${key} — ${meta.description}`;
      sel.appendChild(o);
    }
  } catch {}
}

async function loadStructure() {
  const body = el('structure-body');
  try {
    const data = await apiGet('/api/structure');
    body.innerHTML = '';
    if (!data.files || !data.files.length) { body.innerHTML = '<p class="empty">No symbols indexed yet.</p>'; return; }
    for (const f of data.files) {
      const fn = document.createElement('div');
      fn.className = 'node node--file';
      fn.textContent = f.name;
      body.appendChild(fn);
      for (const child of f.children || []) appendNode(body, child);
    }
  } catch { body.innerHTML = '<p class="empty">Structure unavailable.</p>'; }
}
function appendNode(root, node) {
  const d = document.createElement('div');
  d.className = `node node--${node.type}`;
  d.textContent = node.signature || node.name;
  d.title = `Ln ${node.line_start}-${node.line_end}`;
  d.addEventListener('click', () => { if (findTab(S.active)) gotoLine(node.line_start); });
  root.appendChild(d);
  for (const c of node.children || []) appendNode(root, c);
}

async function loadWebSources() {
  const body = el('web-body');
  try {
    const data = await apiGet('/api/web_sources');
    if (!data.sources || !data.sources.length) { body.innerHTML = '<p class="empty">No URLs fetched this session.</p>'; return; }
    body.innerHTML = '';
    for (const s of data.sources) {
      const d = document.createElement('div');
      d.className = 'source';
      d.innerHTML = `<div class="source__url">${s.url}</div><div class="source__time">${s.status} · ${new Date(s.timestamp).toLocaleTimeString()}</div>`;
      body.appendChild(d);
    }
  } catch {}
}

async function loadTransactions() {
  const body = el('tx-body');
  try {
    const data = await apiGet('/api/transactions');
    if (!data.transactions || !data.transactions.length) {
      body.innerHTML = '<p class="empty">No committed changes this session.</p>';
      return;
    }
    body.innerHTML = '';
    for (const t of data.transactions) {
      const d = document.createElement('div');
      d.className = 'tx-entry';
      const when = t.committed_at ? new Date(t.committed_at * 1000).toLocaleTimeString() : '—';
      const reverted = t.status === 'reverted';
      d.innerHTML = `
        <div class="tx-entry__head">
          <span class="tx-entry__id">${t.tx_id}</span>
          <span class="tx-entry__status${reverted ? ' tx-entry__status--reverted' : ''}">${t.status}</span>
        </div>
        <div class="tx-entry__time">${when}</div>
        <div class="tx-entry__files">${(t.files || []).join(', ') || '(no files)'}</div>`;
      if (!reverted) {
        const btn = document.createElement('button');
        btn.className = 'btn btn--ghost btn--xs tx-entry__revert';
        btn.textContent = 'Revert';
        btn.addEventListener('click', () => revertTransaction(t.tx_id));
        d.appendChild(btn);
      }
      body.appendChild(d);
    }
  } catch {}
}

async function revertTransaction(txId) {
  const r = await apiPost('/api/tx_revert', { tx_id: txId });
  if (r.ok) {
    toast(`Reverted ${txId}`, 'ok');
    await loadFiles();
    await loadTransactions();
    if (S.active) refreshWrittenFile(S.active);
  } else {
    toast(r.error || 'Revert failed', 'error');
  }
}

/* ============================================================
   Diff review modal
   ============================================================ */
function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}

function buildDiffFileNode(f) {
  const added = f.hunks.reduce((n, h) => n + h.new_lines.length, 0);
  const removed = f.hunks.reduce((n, h) => n + h.old_lines.length, 0);
  const node = document.createElement('div');
  node.className = 'diff-file';
  node.dataset.path = f.path;
  const badge = f.is_delete ? 'delete' : f.is_new ? 'new file' : f.mode;
  const warnings = (f.diagnostics || []).filter(d => d.severity !== 'info');
  const warnHtml = warnings.length
    ? `<div class="diff-file__warn">${warnings.map(d => `⚠ ${escapeHtml(d.message)}`).join('<br>')}</div>`
    : '';
  node.innerHTML = `
    <div class="diff-file__head">
      <label class="diff-file__check">
        <input type="checkbox" class="diff-file-toggle" checked>
        <span class="mono">${escapeHtml(f.path)}</span>
      </label>
      <span class="diff-file__badge">${escapeHtml(badge)}</span>
      <span class="diff-file__stat"><span class="add">+${added}</span> <span class="del">-${removed}</span></span>
    </div>
    ${warnHtml}
    <div class="diff-file__hunks"></div>`;
  const hunksWrap = node.querySelector('.diff-file__hunks');
  for (const h of f.hunks) {
    const hn = document.createElement('div');
    hn.className = 'diff-hunk';
    hn.dataset.hunkId = h.id;
    const lines = [
      ...h.old_lines.map(l => `<span class="diff-line diff-line--del">- ${escapeHtml(l)}</span>`),
      ...h.new_lines.map(l => `<span class="diff-line diff-line--add">+ ${escapeHtml(l)}</span>`),
    ].join('');
    hn.innerHTML = `
      <div class="diff-hunk__head">
        <label><input type="checkbox" class="diff-hunk-toggle" checked> @@ line ${h.old_start}</label>
      </div>
      <pre class="diff-hunk__body mono">${lines || '<span class=\"muted\">(empty)</span>'}</pre>`;
    const toggle = hn.querySelector('.diff-hunk-toggle');
    toggle.addEventListener('change', () => hn.classList.toggle('is-off', !toggle.checked));
    hunksWrap.appendChild(hn);
  }
  const fileToggle = node.querySelector('.diff-file-toggle');
  fileToggle.addEventListener('change', () => {
    node.classList.toggle('is-off', !fileToggle.checked);
    hunksWrap.hidden = !fileToggle.checked;
  });
  return node;
}

function openDiffReview(ev) {
  S.pendingDiff = { tx_id: ev.tx_id, files: ev.files || [] };
  el('diff-tx-id').textContent = `tx ${ev.tx_id}`;
  el('diff-summary').textContent = `${(ev.files || []).length} file(s) awaiting review`;
  const list = el('diff-file-list');
  list.innerHTML = '';
  for (const f of ev.files || []) list.appendChild(buildDiffFileNode(f));
  el('diff-review-modal').style.display = 'flex';
  switchPanel('output');
  toast(`Review ${(ev.files || []).length} file change(s) to continue`, 'info', 4000);
}

function closeDiffReview(txId) {
  if (S.pendingDiff && (!txId || S.pendingDiff.tx_id === txId)) {
    el('diff-review-modal').style.display = 'none';
    S.pendingDiff = null;
  }
}

function collectDiffDecision(action) {
  if (action !== 'selective') return { action };
  const files = {};
  $$('.diff-file', el('diff-file-list')).forEach(node => {
    const path = node.dataset.path;
    const accepted = node.querySelector('.diff-file-toggle').checked;
    const rejected_hunks = $$('.diff-hunk', node)
      .filter(h => !h.querySelector('.diff-hunk-toggle').checked)
      .map(h => h.dataset.hunkId);
    files[path] = { accepted, rejected_hunks };
  });
  return { action: 'selective', files };
}

async function resolveDiffReview(action) {
  if (!S.pendingDiff) return;
  const { tx_id } = S.pendingDiff;
  const decision = collectDiffDecision(action);
  closeDiffReview(tx_id);
  try {
    await apiPost('/api/tx_decision', { stream_id: S.streamId, tx_id, decision });
  } catch (e) {
    toast(String(e), 'error');
  }
}

async function reindex() {
  const r = await apiPost('/api/rebuild_index', {});
  toast(`Reindexed · ${r.chunks} chunks`, 'ok');
  loadStructure();
}

async function scaffold(key) {
  if (!key) return;
  const r = await apiPost('/api/template', { template: key });
  if (r.success) { await loadFiles(); if (r.main_file) openFile(r.main_file); toast(`Scaffolded ${key}`, 'ok'); }
  else toast(r.error || 'Template failed', 'error');
  el('template-select').value = '';
}

/* ============================================================
   Views / panels / toggles
   ============================================================ */
function switchView(name) {
  $$('.rail__item').forEach(b => b.classList.toggle('is-active', b.dataset.view === name));
  $$('.sidebar .view').forEach(v => v.classList.toggle('is-active', v.dataset.view === name));
  if (name === 'structure') loadStructure();
  if (name === 'web') loadWebSources();
  if (name === 'transactions') loadTransactions();
}
function switchPanel(name) {
  $$('.panel__tab').forEach(b => b.classList.toggle('is-active', b.dataset.tab === name));
  $$('.panel__body').forEach(b => b.classList.toggle('is-active', b.dataset.tab === name));
}

/* ============================================================
   Wiring
   ============================================================ */
function bind() {
  // editor
  const ta = codeEl();
  ta.addEventListener('input', () => {
    renderGutter();
    updateSyntaxHighlight();
    const tab = findTab(S.active);
    if (tab) { tab.dirty = true; tab.content = ta.value; renderTabs(); }
    validateNow();
    scheduleAutosave();
  });
  ta.addEventListener('blur', () => {
    const tab = findTab(S.active);
    if (tab && tab.dirty) saveActive(true);
  });
  ta.addEventListener('scroll', () => {
    syncEditorScroll();
  });
  ta.addEventListener('keyup', updateCursor);
  ta.addEventListener('click', updateCursor);
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Tab') { e.preventDefault(); const s = ta.selectionStart, en = ta.selectionEnd; ta.value = ta.value.slice(0, s) + '    ' + ta.value.slice(en); ta.selectionStart = ta.selectionEnd = s + 4; renderGutter(); }
    if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); saveActive(); }
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); runActive(); }
  });

  // title bar
  el('btn-run').addEventListener('click', runActive);
  el('btn-sprint').addEventListener('click', () => sendPrompt(true));
  el('btn-stop').addEventListener('click', stopStream);
  el('toggle-sidebar').addEventListener('click', (e) => toggleShell('no-side', e.currentTarget));
  el('toggle-panel').addEventListener('click', (e) => toggleShell('no-panel', e.currentTarget));
  el('toggle-inspector').addEventListener('click', (e) => toggleShell('no-insp', e.currentTarget));

  // rail
  $$('.rail__item').forEach(b => b.addEventListener('click', () => switchView(b.dataset.view)));

  // explorer
  el('btn-newfile').addEventListener('click', newFile);
  el('btn-reindex').addEventListener('click', reindex);
  el('template-select').addEventListener('change', (e) => scaffold(e.target.value));

  // structure / web
  el('btn-structure-refresh').addEventListener('click', loadStructure);
  el('btn-clear-web').addEventListener('click', async () => { await apiPost('/api/clear_web_cache', {}); loadWebSources(); });

  // panel tabs
  $$('.panel__tab').forEach(b => b.addEventListener('click', () => switchPanel(b.dataset.tab)));
  el('btn-clear-panel').addEventListener('click', () => {
    const active = $('.panel__tab.is-active')?.dataset.tab;
    if (active === 'terminal') el('term-log').innerHTML = '';
    else if (active === 'output') el('output-log').innerHTML = '';
    else if (active === 'reasoning') el('reasoning-log').textContent = '';
    else if (active === 'wire') el('wire-log').innerHTML = '';
    else clearProblems();
  });

  // terminal
  el('term-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const input = el('term-cmd');
    const cmd = input.value.trim();
    if (cmd) { runTerminal(cmd); input.value = ''; }
  });

  // stream overlay
  el('stream-detach').addEventListener('click', hideStreamOverlay);

  // composer
  el('composer').addEventListener('submit', (e) => { e.preventDefault(); sendPrompt(false); });
  el('btn-attach').addEventListener('click', (e) => { e.stopPropagation(); toggleAttachMenu(); });
  document.addEventListener('click', (e) => {
    const menu = el('attach-menu');
    const btn = el('btn-attach');
    if (!menu.hidden && !menu.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
      menu.hidden = true;
    }
  });
  window.addEventListener('beforeunload', () => {
    const tab = findTab(S.active);
    if (tab && tab.dirty) {
      navigator.sendBeacon('/api/save',
        new Blob([JSON.stringify({ filename: tab.path, code: tab.content })], { type: 'application/json' }));
    }
  });
  el('prompt').addEventListener('input', (e) => {
    const toks = Math.max(1, Math.round(e.target.value.length / 3.6));
    el('composer-tokens').textContent = `${e.target.value ? toks : 0} tok`;
  });
  el('prompt').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent?.isComposing && e.keyCode !== 229) {
      e.preventDefault(); sendPrompt(false);
    }
  });

  // telemetry toggle
  el('tel-toggle').addEventListener('click', toggleTelemetry);

  // settings
  el('model-select').addEventListener('change', async (e) => { await apiPost('/api/model', { model: e.target.value }); loadHealth(); });
  el('runmode-select').addEventListener('change', async (e) => { const r = await apiPost('/api/run_mode', { mode: e.target.value }); S.runMode = r.run_mode; el('status-run').textContent = r.run_mode; });
  el('opt-autorun').addEventListener('change', async (e) => { const r = await apiPost('/api/auto_run', { enabled: e.target.checked }); S.autoRun = r.auto_run; });
  el('opt-thinking').addEventListener('change', async (e) => { S.thinking = e.target.checked; await apiPost('/api/thinking', { enabled: e.target.checked }); });
  el('opt-autoapply').addEventListener('change', (e) => { S.autoApply = e.target.checked; });
  el('opt-follow').addEventListener('change', (e) => { S.follow = e.target.checked; });
  el('btn-unload').addEventListener('click', async () => { const r = await apiPost('/api/unload', {}); toast(r.unloaded ? 'Model unloaded' : 'Unload failed', r.unloaded ? 'ok' : 'error'); });
}

function toggleShell(cls, btn) {
  const app = el('app');
  app.classList.toggle(cls);
  const pressed = !app.classList.contains(cls);
  btn.setAttribute('aria-pressed', pressed ? 'true' : 'false');
}

/* ============================================================
   Runtime styles for the cleaned chat answer
   (kept here so no separate stylesheet edit is required)
   ============================================================ */
function injectStyles() {
  if (el('ca-styles')) return;
  const s = document.createElement('style');
  s.id = 'ca-styles';
  s.textContent = `
    .msg--ai .ca-p { margin: 0 0 .5em; line-height: 1.55; }
    .msg--ai .ca-p:last-child { margin-bottom: 0; }
    .msg--ai .ca-h { margin: .2em 0 .4em; font-size: 13px; font-weight: 600; color: var(--text); }
    .msg--ai .ca-ul { margin: 0 0 .5em; padding-left: 1.1em; }
    .msg--ai .ca-ul li { margin: .12em 0; }
    .msg--ai code { font-family: var(--font-mono); font-size: 12px; background: var(--elev-2); border: 1px solid var(--line-soft); border-radius: var(--r-sm); padding: 0 .3em; }
    .msg--ai a { color: var(--primary); text-decoration: none; }
    .msg--ai a:hover { text-decoration: underline; }
    .msg--ai .ca-muted { color: var(--text-mute); }
  `;
  document.head.appendChild(s);
}


/* ============================================================
   Python Syntax Highlighter
   ============================================================ */

function highlightPython(code) {
  if (!code) return '';

  // Escape HTML once, up front. Every rule below only ever *wraps* pieces
  // of this already-escaped text in a single linear pass — it never
  // re-scans text it (or an earlier rule) already emitted. The previous
  // version ran each rule as its own global find/replace over the whole
  // growing string, so a later rule (e.g. the string matcher) could match
  // text *inside* a span an earlier rule had just inserted — e.g. the
  // quoted class name inside <span class="sh-comment"> — corrupting the
  // markup into visible tag soup instead of highlighted code.
  const escaped = code
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  const CONTROL = new Set(['if', 'else', 'elif', 'for', 'while', 'try', 'except', 'finally', 'with', 'break', 'continue', 'pass', 'raise', 'return', 'yield', 'async', 'await']);
  const KEYWORD = new Set(['def', 'class', 'lambda', 'import', 'from', 'as', 'global', 'nonlocal', 'assert', 'del']);
  const BOOL = new Set(['True', 'False', 'None']);
  const SELF = new Set(['self', 'cls']);
  const BUILTIN = new Set(['print', 'len', 'range', 'enumerate', 'zip', 'map', 'filter', 'sum', 'min', 'max', 'sorted', 'reversed', 'open', 'input', 'int', 'str', 'float', 'list', 'dict', 'tuple', 'set', 'bool', 'type', 'isinstance', 'hasattr', 'getattr', 'setattr', 'dir', 'help', 'super', 'abs', 'round', 'pow', 'divmod', 'chr', 'ord', 'hex', 'oct', 'bin', 'format', 'repr', 'vars', 'locals', 'globals', 'eval', 'exec', 'compile', 'callable', 'staticmethod', 'classmethod', 'property', 'iter', 'next', 'slice', 'all', 'any', 'complex', 'bytes', 'bytearray', 'memoryview', 'frozenset']);
  const TYPES = new Set(['List', 'Dict', 'Tuple', 'Set', 'Optional', 'Union', 'Any', 'Callable', 'Iterator', 'Iterable', 'Generator', 'TypeVar', 'Generic', 'Protocol', 'NamedTuple', 'TypedDict', 'Final', 'Literal', 'ClassVar', 'Pattern', 'Match', 'TextIO', 'BinaryIO']);

  // One regex, tried left-to-right at every position: comment | string |
  // decorator | number | identifier(+optional trailing "("). Whatever
  // doesn't match any branch is left untouched by replace().
  const TOKEN = /(#.*$)|("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|(@[A-Za-z_][A-Za-z0-9_.]*)|(\b\d+\.?\d*\b)|([A-Za-z_][A-Za-z0-9_]*)(\s*)(\()?/gm;

  return escaped.replace(TOKEN, (m, comment, str, deco, num, ident, ws, paren) => {
    if (comment !== undefined) return `<span class="sh-comment">${comment}</span>`;
    if (str !== undefined) return `<span class="sh-string">${str}</span>`;
    if (deco !== undefined) return `<span class="sh-decorator">${deco}</span>`;
    if (num !== undefined) return `<span class="sh-number">${num}</span>`;
    if (ident !== undefined) {
      let cls = null;
      if (CONTROL.has(ident)) cls = 'sh-keyword-control';
      else if (KEYWORD.has(ident)) cls = 'sh-keyword';
      else if (BOOL.has(ident)) cls = 'sh-bool';
      else if (SELF.has(ident)) cls = 'sh-self';
      else if (paren) cls = 'sh-function';
      else if (BUILTIN.has(ident)) cls = 'sh-builtin';
      else if (TYPES.has(ident)) cls = 'sh-type';
      const out = cls ? `<span class="${cls}">${ident}</span>` : ident;
      return out + (ws || '') + (paren || '');
    }
    return m;
  });
}

function updateSyntaxHighlight() {
  const ta = el('code');
  const hl = el('code-highlight');
  if (!ta || !hl) return;
  hl.querySelector('code').innerHTML = highlightPython(ta.value);
}

function syncEditorScroll() {
  const ta = el('code');
  const scroll = el('editor-scroll');
  const hl = el('code-highlight');
  const gutter = el('gutter');
  if (!ta || !scroll || !hl) return;
  hl.style.transform = 'translate(-' + scroll.scrollLeft + 'px, -' + scroll.scrollTop + 'px)';
  if (gutter) gutter.style.transform = 'translate(0px, ' + scroll.scrollTop + 'px)';
}

/* ============================================================
   Boot
   ============================================================ */
async function init() {
  injectStyles();
  bind();

  el('chip-model').addEventListener('click', () => {
    el('model-manager-modal').style.display = 'flex';
  });
  el('chip-model').style.cursor = 'pointer';

  el('btn-close-manager').addEventListener('click', () => {
    el('model-manager-modal').style.display = 'none';
  });

  el('diff-apply-all').addEventListener('click', () => resolveDiffReview('apply_all'));
  el('diff-reject-all').addEventListener('click', () => resolveDiffReview('reject_all'));
  el('diff-close').addEventListener('click', () => resolveDiffReview('reject_all'));
  el('diff-commit-selective').addEventListener('click', () => resolveDiffReview('selective'));
  el('btn-tx-refresh').addEventListener('click', loadTransactions);
  
  // Listen for messages from the iframe
  window.addEventListener('message', async (e) => {
    if (e.data && e.data.type === 'MODEL_SELECTED') {
      const { model, url, key, auth } = e.data;
      await apiPost('/api/model', { 
        model: model,
        host: url,
        api_key: key,
        auth_type: auth
      });
      await loadHealth();
      await loadModels();
      el('model-manager-modal').style.display = 'none';
      toast(`Switched to ${model}`, 'ok');
    }
  });

  renderGutter();
  updateSyntaxHighlight();
  renderTabs();
  setupSpark();
  window.addEventListener('resize', debounce(setupSpark, 200));
  await loadHealth();
  await Promise.all([loadFiles(), loadModels(), loadTemplates()]);
  // open a sensible default
  if (S.files.find(f => f.path === 'main.py')) openFile('main.py');
  else if (S.files[0]) openFile(S.files[0].path);
  // poll health every 15s
  setInterval(loadHealth, 15000);
}

document.addEventListener('DOMContentLoaded', init);
