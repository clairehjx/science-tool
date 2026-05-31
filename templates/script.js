// Tab switching
function showTab(id) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  document.querySelector('[data-tab-id="' + id + '"]').classList.add('active');
  window.scrollTo({ top: 0, behavior: 'smooth' });
  // ECharts initialises inside display:none so has zero dimensions — resize each instance after reflow
  setTimeout(function() {
    document.querySelectorAll('[id^="ec-"]').forEach(function(el) {
      var inst = echarts.getInstanceByDom(el);
      if (inst) inst.resize();
    });
  }, 50);
}

// Show/hide model answer
function toggleAnswer(id) {
  const box = document.getElementById('answer-' + id);
  const btn = document.querySelector('[data-answer="' + id + '"]');
  if (!box || !btn) return;
  const hidden = box.style.display === 'none' || box.style.display === '';
  box.style.display = hidden ? 'block' : 'none';
  btn.textContent = hidden ? 'Hide Model Answer' : 'Show Model Answer';
  btn.style.background = hidden
    ? 'linear-gradient(135deg, #FF6B6B, #FF8585)'
    : 'linear-gradient(135deg, #4ECDC4, #6BCBC4)';
}

// Flip cards (Wrong Answers tab)
function flipCard(id) {
  const card = document.getElementById('card-' + id);
  if (card) card.classList.toggle('flipped');
}

// Keyword filter buttons
function filterKeywords(cat, btn) {
  document.querySelectorAll('.keyword-card').forEach(c => {
    c.style.display = (cat === 'all' || c.dataset.category === cat) ? 'block' : 'none';
  });
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

// Progress tracking
let studiedItems = {};
function markStudied(id) {
  const cb = document.getElementById(id);
  if (cb) studiedItems[id] = cb.checked;
  updateAllProgress();
}

function updateMotivation(pct) {
  const el = document.getElementById('motivation-msg');
  if (!el) return;
  if (pct === 0)        el.textContent = 'Start practising to improve your {LEVEL} open-ended answers!';
  else if (pct < 25)    el.textContent = 'Good start! Keep practising your written answers for {LEVEL}!';
  else if (pct < 50)    el.textContent = 'Making progress! Your {LEVEL} open-ended skills are improving!';
  else if (pct < 75)    el.textContent = 'Over halfway! You are building strong {LEVEL} writing skills!';
  else if (pct < 100)   el.textContent = 'Almost done! You are well prepared for the {LEVEL} open-ended section!';
  else                  el.textContent = 'Excellent! You are ready for the {LEVEL} open-ended section. All the best!';
}

function updateAllProgress() {
  const cats   = { questions: 0, mistakes: 0, traps: 0, distinctions: 0 };
  const totals = { questions: 0, mistakes: 0, traps: 0, distinctions: 0 };
  document.querySelectorAll('input[type=checkbox][id]').forEach(cb => {
    const cat = cb.id.split('-')[0];
    if (cats[cat] !== undefined) {
      totals[cat]++;
      if (cb.checked) cats[cat]++;
    }
  });
  Object.keys(cats).forEach(cat => {
    const el  = document.getElementById(cat + '-progress');
    const tot = document.getElementById(cat + '-total');
    const bar = document.getElementById(cat + '-bar');
    if (el)  el.textContent  = cats[cat];
    if (tot) tot.textContent = totals[cat];
    if (bar) bar.style.width = (totals[cat] ? Math.round(cats[cat] / totals[cat] * 100) : 0) + '%';
  });
  const total = Object.values(totals).reduce((a, b) => a + b, 0);
  const done  = Object.values(cats).reduce((a, b) => a + b, 0);
  const pct   = total ? Math.round(done / total * 100) : 0;
  const fill  = document.getElementById('overall-fill');
  const text  = document.getElementById('overall-text');
  if (fill) fill.style.width   = pct + '%';
  if (text) text.textContent   = pct + '% studied';
  updateMotivation(pct);
}

function resetProgress() {
  document.querySelectorAll('input[type=checkbox]').forEach(cb => { cb.checked = false; });
  studiedItems = {};
  updateAllProgress();
}

// Item-by-item navigation (Top Questions, Common Mistakes, etc.)
let navState = {};
function navigateItem(type, direction) {
  if (navState[type] === undefined) navState[type] = 0;
  const items = document.querySelectorAll('.' + type + '-item');
  navState[type] = Math.max(0, Math.min(items.length - 1, navState[type] + direction));
  items.forEach((item, i) => item.style.display = i === navState[type] ? 'block' : 'none');
  updateNavButtons(type, navState[type], items.length);
}
function updateNavButtons(type, current, total) {
  const prev    = document.getElementById(type + '-prev');
  const next    = document.getElementById(type + '-next');
  const counter = document.getElementById(type + '-counter');
  if (prev)    prev.disabled       = current === 0;
  if (next)    next.disabled       = current === total - 1;
  if (counter) counter.textContent = (current + 1) + ' of ' + total;
}
function initNavigation() {
  ['question', 'mistake', 'trap', 'distinction'].forEach(type => {
    const items = document.querySelectorAll('.' + type + '-item');
    if (items.length > 1) {
      navState[type] = 0;
      items.forEach((item, i) => item.style.display = i === 0 ? 'block' : 'none');
      updateNavButtons(type, 0, items.length);
    }
  });
}

// Fullscreen lightbox for ConceptViz images (handles both <img> and inline <svg>)
function openFullscreen(el) {
  const overlay = document.createElement('div');
  overlay.style.cssText = [
    'position:fixed', 'inset:0', 'z-index:9999',
    'background:rgba(0,0,0,0.93)',
    'display:flex', 'align-items:center', 'justify-content:center',
    'padding:16px', 'cursor:zoom-out',
  ].join(';');
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-label', 'Full screen diagram — press Escape or tap to close');

  const isSvg = el && el.tagName && el.tagName.toLowerCase() === 'svg';
  let big;
  if (isSvg) {
    big = el.cloneNode(true);
    big.removeAttribute('onclick');
    big.style.cssText = [
      'max-width:100%', 'max-height:100%', 'width:auto', 'height:auto',
      'border-radius:10px', 'background:white',
      'box-shadow:0 8px 48px rgba(0,0,0,0.5)',
    ].join(';');
  } else {
    big = document.createElement('img');
    big.src = el.src;
    big.alt = el.alt || '';
    big.style.cssText = [
      'max-width:100%', 'max-height:100%', 'object-fit:contain',
      'border-radius:10px', 'box-shadow:0 8px 48px rgba(0,0,0,0.5)',
    ].join(';');
  }

  const hint = document.createElement('div');
  hint.textContent = 'Tap or press Esc to close';
  hint.style.cssText = [
    'position:absolute', 'bottom:20px', 'left:50%', 'transform:translateX(-50%)',
    'color:rgba(255,255,255,0.55)', 'font-size:12px', 'pointer-events:none',
  ].join(';');

  overlay.appendChild(big);
  overlay.appendChild(hint);
  overlay.onclick = () => overlay.remove();

  function onKey(e) {
    if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', onKey); }
  }
  document.addEventListener('keydown', onKey);
  document.body.appendChild(overlay);
}

// ── Sortable Tailwind table helper (tsTable) ─────────────────────────────────
const _tsTbl = {};
function tsTable(id, headers, rows) {
  _tsTbl[id] = { headers, rows, sc: -1, sd: 1 };
  _tsRender(id);
}
function _tsRender(id) {
  const s = _tsTbl[id], el = document.getElementById(id);
  if (!s || !el) return;
  let data = s.rows;
  if (s.sc >= 0) {
    data = [...data].sort((a, b) => {
      const av = a[s.sc], bv = b[s.sc];
      const an = +av, bn = +bv;
      return (isNaN(an) || isNaN(bn) ? String(av).localeCompare(String(bv)) : an - bn) * s.sd;
    });
  }
  const icon = i => s.sc === i ? (s.sd > 0 ? ' ▲' : ' ▼') : ' <span style="opacity:.35">↕</span>';
  el.innerHTML =
    '<div style="overflow-x:auto;border-radius:12px;border:1px solid #E5E9F0;margin:10px 0">' +
    '<table style="width:100%;border-collapse:collapse;font-size:13px">' +
    '<thead><tr style="background:#EEF4FF">' +
    s.headers.map((h, i) =>
      '<th style="padding:10px 14px;text-align:left;font-weight:700;color:#4D96FF;' +
      'cursor:pointer;user-select:none;white-space:nowrap" onclick="_tsClick(\'' + id + '\',' + i + ')">' +
      h + icon(i) + '</th>'
    ).join('') +
    '</tr></thead><tbody>' +
    data.map((r, ri) =>
      '<tr style="border-top:1px solid #F0F2F5;background:' + (ri % 2 === 0 ? '#fff' : '#FAFBFF') + '">' +
      r.map((c, ci) =>
        '<td style="padding:9px 14px;' + (ci === 0 ? 'font-weight:600;color:#2D3748' : 'color:#4A5568') + '">' + c + '</td>'
      ).join('') + '</tr>'
    ).join('') +
    '</tbody></table></div>';
}
function _tsClick(id, col) {
  const s = _tsTbl[id];
  if (!s) return;
  if (s.sc === col) s.sd *= -1; else { s.sc = col; s.sd = 1; }
  _tsRender(id);
}

// ── Global Rough.js helpers (used by formula and sketch diagrams) ─────────────
function rvt(s, txt, x, y, o) {
  const e = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  e.setAttribute('x', x); e.setAttribute('y', y);
  e.setAttribute('text-anchor', o && o.a || 'middle');
  e.setAttribute('font-size',   o && o.fs || '12');
  e.setAttribute('fill',        o && o.c  || '#2D3748');
  if (o && o.b) e.setAttribute('font-weight', 'bold');
  e.textContent = txt; s.appendChild(e);
}
function rvah(s, x, y, dir, color) {
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
  p.setAttribute('points', (dir || 'r') === 'd'
    ? `${x-5},${y-10} ${x+5},${y-10} ${x},${y}`
    : `${x-10},${y-5} ${x},${y} ${x-10},${y+5}`);
  p.setAttribute('fill', color || '#FFA500'); s.appendChild(p);
}
function rvNode(s, rc, x, y, w, h, fill, stroke, label, sub) {
  s.appendChild(rc.rectangle(x, y, w, h,
    { fill, fillStyle: 'solid', roughness: 1.2, stroke, strokeWidth: 2 }));
  const ly = sub ? y + h / 2 - 4 : y + h / 2 + 5;
  rvt(s, label, x + w / 2, ly, { b: true });
  if (sub) rvt(s, sub, x + w / 2, y + h / 2 + 11, { fs: '10', c: '#666' });
}

// ── Card helpers — Phase A token reduction ───────────────────────────────────
// Each helper appends a fully-formed card to its tab's standard container id,
// matching the existing CSS classes so styles.css and event handlers keep
// working unchanged (filterKeywords, flipCard delegation, updateAllProgress).
function _dAppend(containerId, html) {
  const el = document.getElementById(containerId);
  if (!el) { console.warn('[card-helper] missing container #' + containerId); return; }
  el.insertAdjacentHTML('beforeend', html);
}
function _dEsc(s) {
  return (s == null ? '' : String(s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Shared palette for SVG/card helpers (dPal). Consumed by all d-prefixed helpers.
const dPal = {
  blue:'#4D96FF', teal:'#4ECDC4', red:'#FF6B6B', yellow:'#FFE066',
  orange:'#FFA500', green:'#2ECC71', purple:'#B388FF',
  bgBlue:'#E5F5FF', bgGreen:'#E5FFE5', bgRed:'#FFE5E5', bgYellow:'#FFF8E5',
  wrongBg:'#fff5f5', correctBg:'#f0fff8',
  textDark:'#2D3748', textMute:'#666', stroke:'#E5E9F0'
};

// kCard — Keyword card. Container: #keywords-list (.keyword-grid)
// Args: {id, word, tip, cat, freq, usage}
//   cat ∈ {action,descriptive,linking,technical} (drives data-category for filter)
//   word/tip → keyword span; freq → keyword-freq line; usage may contain HTML
function kCard(o) {
  const catLabel = o.cat ? o.cat.charAt(0).toUpperCase() + o.cat.slice(1) : '';
  _dAppend('keywords-list',
    '<div class="keyword-card" data-category="' + (o.cat || '') + '">' +
      '<span class="keyword" data-tip="' + _dEsc(o.tip || '') + '">' + _dEsc(o.word) + '</span>' +
      (o.cat ? '<span class="badge badge-' + o.cat + '">' + catLabel + '</span>' : '') +
      (o.freq ? '<div class="keyword-freq">' + _dEsc(o.freq) + '</div>' : '') +
      (o.usage ? '<div class="keyword-usage">' + o.usage + '</div>' : '') +
    '</div>');
}

// mCard — Mistake card. Container: #mistakes-list
// Args: {id, title, marks, wrong, missing, right, keyword, fix, diagram?}
//   id → checkbox id "mistakes-{id}-cb" (drives updateAllProgress)
//   wrong/right may contain inline HTML (e.g. <span class="keyword">)
//   diagram is an optional pre-built HTML string (e.g. an inline <svg>)
function mCard(o) {
  _dAppend('mistakes-list',
    '<div class="mistake-item"><div class="card">' +
      '<h3 style="margin-bottom:10px">Mistake ' + o.id + ': ' + _dEsc(o.title) + '</h3>' +
      '<div class="comparison">' +
        '<div>' +
          '<strong style="color:#FF6B6B">What Students Write</strong>' +
          '<div class="speech-bubble-wrong">"' + o.wrong + '"</div>' +
          (o.missing ? '<span class="chip chip-missing">' + _dEsc(o.missing) + '</span>' : '') +
          '<div class="marks-badge marks-wrong" style="margin-top:8px">0/' + o.marks + ' marks</div>' +
        '</div>' +
        '<div>' +
          '<strong style="color:#2ECC71">What to Write Instead</strong>' +
          '<div class="speech-bubble-correct">"' + o.right + '"</div>' +
          (o.keyword ? '<span class="chip chip-keyword">' + _dEsc(o.keyword) + '</span>' : '') +
          '<div class="marks-badge marks-correct" style="margin-top:8px">' + o.marks + '/' + o.marks + ' marks</div>' +
        '</div>' +
      '</div>' +
      (o.diagram ? '<div class="diagram-container">' + o.diagram + '</div>' : '') +
      (o.fix ? '<div class="fix-box"><strong>The Fix:</strong> ' + o.fix + '</div>' : '') +
      '<label><input type="checkbox" id="mistakes-' + o.id + '-cb" onchange="markStudied(this.id)"> Reviewed</label>' +
    '</div></div>');
}

// tCard — Trap flip-card. Container: #traps-list
// Args: {id, trap, description, truth, whyMatters}
//   trap = wrong-belief headline; truth/whyMatters may contain inline HTML
//   No checkbox — matches existing trap-item structure. Flip handled by
//   delegated click handler in DOMContentLoaded.
function tCard(o) {
  _dAppend('traps-list',
    '<div class="trap-item"><div class="flip-card"><div class="flip-card-inner">' +
      '<div class="flip-front" style="background:#FFF5F5;border:2px solid #FF6B6B">' +
        '<div class="wrong"><strong>Trap ' + o.id + ': ' + _dEsc(o.trap) + '</strong></div>' +
        '<p style="margin-top:8px;color:#555;font-size:13px">' + _dEsc(o.description) + '</p>' +
        '<div class="flip-hint">Tap to reveal the truth</div>' +
      '</div>' +
      '<div class="flip-back" style="background:#F0FFF8;border:2px solid #4ECDC4">' +
        '<div class="correct"><strong>The Truth</strong></div>' +
        '<p style="margin-top:8px;font-size:13px">' + o.truth + '</p>' +
        (o.whyMatters ? '<div class="link-word-box" style="margin-top:10px;font-size:12px">' + o.whyMatters + '</div>' : '') +
      '</div>' +
    '</div></div></div>');
}

// dCardCompare — Distinction (A vs B) card. Container: #distinctions-list
// Args: {id, leftLabel, rightLabel, leftItems[], rightItems[],
//        leftDiagram?, rightDiagram?, note?, trap?}
//   id → checkbox id "distinctions-{id}-cb"
//   leftItems/rightItems are arrays of <li> contents (HTML allowed)
function dCardCompare(o) {
  const lis = arr => (arr || []).map(s => '<li>' + s + '</li>').join('');
  _dAppend('distinctions-list',
    '<div class="distinction-item"><div class="card">' +
      '<h3>' + _dEsc(o.leftLabel) + ' vs ' + _dEsc(o.rightLabel) + '</h3>' +
      '<div class="comparison">' +
        '<div style="background:#E5F5FF;border-radius:10px;padding:14px">' +
          '<strong style="color:#4D96FF">' + _dEsc(o.leftLabel) + '</strong>' +
          (o.leftDiagram ? '<div class="diagram-container" style="margin:10px 0">' + o.leftDiagram + '</div>' : '') +
          '<ul>' + lis(o.leftItems) + '</ul>' +
        '</div>' +
        '<div style="background:#E5FFE5;border-radius:10px;padding:14px">' +
          '<strong style="color:#2ECC71">' + _dEsc(o.rightLabel) + '</strong>' +
          (o.rightDiagram ? '<div class="diagram-container" style="margin:10px 0">' + o.rightDiagram + '</div>' : '') +
          '<ul>' + lis(o.rightItems) + '</ul>' +
        '</div>' +
      '</div>' +
      (o.note ? '<div class="link-word-box"><strong>How to Write the Compare Answer:</strong><br>' + o.note + '</div>' : '') +
      (o.trap ? '<div class="warning"><strong>Why Students Confuse These:</strong> ' + o.trap + '</div>' : '') +
      '<label><input type="checkbox" id="distinctions-' + o.id + '-cb" onchange="markStudied(this.id)"> Mastered</label>' +
    '</div></div>');
}

// ── Tier 1 frame helpers (SVG diagrams) ──────────────────────────────────────
// Each writes a complete inline <svg> into the element with the given id and
// includes a one-time <defs> block with arrowhead markers. Helpers return the
// full SVG string for testability; the public versions also mount it.
const _D_DEFS =
  '<defs>' +
    '<marker id="d-arr-r" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">' +
      '<polygon points="0 0,6 3,0 6" fill="' + dPal.red + '"/></marker>' +
    '<marker id="d-arr-b" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">' +
      '<polygon points="0 0,6 3,0 6" fill="' + dPal.blue + '"/></marker>' +
    '<marker id="d-arr-o" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">' +
      '<polygon points="0 0,6 3,0 6" fill="' + dPal.orange + '"/></marker>' +
    '<marker id="d-arr-g" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">' +
      '<polygon points="0 0,6 3,0 6" fill="' + dPal.green + '"/></marker>' +
  '</defs>';

function _dMount(id, svg) {
  const el = document.getElementById(id);
  if (!el) { console.warn('[d-helper] missing #' + id); return; }
  el.innerHTML = svg;
}

// dCompare(id, leftBody, rightBody, opts?) — two-panel mistake comparison.
// viewBox 440×155. Left panel x=0..220 (#fff5f5/#FF6B6B), right panel x=220..440
// (#f0fff8/#4ECDC4). Bodies are SVG fragment strings in panel-local
// coordinates (translated automatically). opts: {leftLabel, rightLabel}.
function dCompare(id, leftBody, rightBody, opts) {
  const o = opts || {};
  const svg =
    '<svg viewBox="0 0 440 155" width="100%" style="max-width:100%;height:auto;display:block;margin:0 auto">' +
      _D_DEFS +
      '<rect x="2" y="2" width="216" height="151" rx="6" fill="' + dPal.wrongBg + '" stroke="' + dPal.red + '" stroke-width="1.5"/>' +
      '<rect x="222" y="2" width="216" height="151" rx="6" fill="' + dPal.correctBg + '" stroke="' + dPal.teal + '" stroke-width="1.5"/>' +
      (o.leftLabel ? '<text x="110" y="18" text-anchor="middle" font-size="11" font-weight="700" fill="' + dPal.red + '">' + o.leftLabel + '</text>' : '') +
      (o.rightLabel ? '<text x="330" y="18" text-anchor="middle" font-size="11" font-weight="700" fill="' + dPal.teal + '">' + o.rightLabel + '</text>' : '') +
      '<g>' + (Array.isArray(leftBody) ? leftBody.join('') : (leftBody || '')) + '</g>' +
      '<g transform="translate(220,0)">' + (Array.isArray(rightBody) ? rightBody.join('') : (rightBody || '')) + '</g>' +
    '</svg>';
  _dMount(id, svg);
}

// dDistinct(id, side, body, opts?) — single distinction panel 220×150.
// side: 'left' (blue bg #E5F5FF/#4D96FF) or 'right' (green #E5FFE5/#2ECC71).
function dDistinct(id, side, body, opts) {
  const isLeft = side === 'left' || side == null;
  const bg = isLeft ? dPal.bgBlue : dPal.bgGreen;
  const stroke = isLeft ? dPal.blue : dPal.green;
  const o = opts || {};
  const svg =
    '<svg viewBox="0 0 220 150" width="100%" style="max-width:100%;height:auto;display:block;margin:0 auto">' +
      _D_DEFS +
      '<rect x="2" y="2" width="216" height="146" rx="6" fill="' + bg + '" stroke="' + stroke + '" stroke-width="1.5"/>' +
      (o.title ? '<text x="110" y="16" text-anchor="middle" font-size="11" font-weight="700" fill="' + stroke + '">' + o.title + '</text>' : '') +
      (Array.isArray(body) ? body.join('') : (body || '')) +
    '</svg>';
  _dMount(id, svg);
}

// dScene(id, body, opts?) — full-canvas question scenario diagram 500×280.
// opts: {title, bg ('#E5F5FF' default), w, h}.
function dScene(id, body, opts) {
  const o = opts || {};
  const w = o.w || 500, h = o.h || 280;
  const svg =
    '<svg viewBox="0 0 ' + w + ' ' + h + '" width="100%" style="max-width:100%;height:auto;display:block;margin:0 auto">' +
      _D_DEFS +
      '<rect x="0" y="0" width="' + w + '" height="' + h + '" rx="6" fill="' + (o.bg || dPal.bgBlue) + '"/>' +
      (o.title ? '<text x="' + (w/2) + '" y="20" text-anchor="middle" font-size="13" font-weight="700" fill="' + dPal.textDark + '">' + o.title + '</text>' : '') +
      (Array.isArray(body) ? body.join('') : (body || '')) +
    '</svg>';
  _dMount(id, svg);
}

// dFlow(id, nodes, edges, opts?) — process/concept flow 500×200.
// nodes: [{x,y,w,h,fill,stroke,label,sub?}]
// edges: [{from,to,label?,color?}] — from/to are node indices
// opts: {w,h}
function dFlow(id, nodes, edges, opts) {
  const o = opts || {};
  const w = o.w || 500, h = o.h || 200;
  const ns = (nodes || []).map(n => {
    const cx = n.x + n.w / 2, cy = n.y + n.h / 2;
    const ly = n.sub ? cy - 4 : cy + 4;
    return '<rect x="' + n.x + '" y="' + n.y + '" width="' + n.w + '" height="' + n.h + '" rx="6" fill="' + (n.fill || dPal.bgBlue) + '" stroke="' + (n.stroke || dPal.blue) + '" stroke-width="1.5"/>' +
      '<text x="' + cx + '" y="' + ly + '" text-anchor="middle" font-size="12" font-weight="700" fill="' + dPal.textDark + '">' + n.label + '</text>' +
      (n.sub ? '<text x="' + cx + '" y="' + (cy + 9) + '" text-anchor="middle" font-size="10" fill="' + dPal.textMute + '">' + n.sub + '</text>' : '');
  }).join('');
  const es = (edges || []).map(e => {
    const a = nodes[e.from], b = nodes[e.to];
    if (!a || !b) return '';
    const x1 = a.x + a.w, y1 = a.y + a.h / 2;
    const x2 = b.x, y2 = b.y + b.h / 2;
    const color = e.color || dPal.orange;
    const marker = color === dPal.red ? 'd-arr-r' : color === dPal.blue ? 'd-arr-b' : color === dPal.green ? 'd-arr-g' : 'd-arr-o';
    const lx = x1 + (x2 - x1) * 0.45;
    const ly = y1 + (y2 - y1) * 0.45 - 4;
    return '<line x1="' + x1 + '" y1="' + y1 + '" x2="' + (x2 - 4) + '" y2="' + y2 + '" stroke="' + color + '" stroke-width="2" marker-end="url(#' + marker + ')"/>' +
      (e.label ? '<text x="' + lx + '" y="' + ly + '" text-anchor="middle" font-size="10" font-weight="700" fill="' + color + '">' + e.label + '</text>' : '');
  }).join('');
  const svg =
    '<svg viewBox="0 0 ' + w + ' ' + h + '" width="100%" style="max-width:100%;height:auto;display:block;margin:0 auto">' +
      _D_DEFS + ns + es +
    '</svg>';
  _dMount(id, svg);
}

// ── Generic SVG sub-shapes (always available) ────────────────────────────────
// Pure string returns — compose into Tier-1 frame bodies.
function dArrow(x1, y1, x2, y2, color, label) {
  const c = color || dPal.orange;
  const marker = c === dPal.red ? 'd-arr-r' : c === dPal.blue ? 'd-arr-b' : c === dPal.green ? 'd-arr-g' : 'd-arr-o';
  return '<line x1="' + x1 + '" y1="' + y1 + '" x2="' + x2 + '" y2="' + y2 + '" stroke="' + c + '" stroke-width="2" marker-end="url(#' + marker + ')"/>' +
    (label ? '<text x="' + ((x1 + x2) / 2) + '" y="' + (Math.min(y1, y2) - 4) + '" text-anchor="middle" font-size="10" font-weight="700" fill="' + c + '">' + label + '</text>' : '');
}
function dLabel(x, y, txt, opts) {
  const o = opts || {};
  return '<text x="' + x + '" y="' + y + '" text-anchor="' + (o.a || 'middle') + '" font-size="' + (o.fs || 11) + '"' +
    (o.b ? ' font-weight="700"' : '') + ' fill="' + (o.c || dPal.textDark) + '">' + txt + '</text>';
}
function dBox(x, y, w, h, opts) {
  const o = opts || {};
  return '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="' + (o.r != null ? o.r : 6) + '" fill="' + (o.fill || 'white') + '" stroke="' + (o.stroke || dPal.blue) + '" stroke-width="' + (o.sw || 1.5) + '"/>' +
    (o.label ? '<text x="' + (x + w / 2) + '" y="' + (y + h / 2 + 4) + '" text-anchor="middle" font-size="11" font-weight="700" fill="' + dPal.textDark + '">' + o.label + '</text>' : '');
}
function dDashedLine(x1, y1, x2, y2, color) {
  return '<line x1="' + x1 + '" y1="' + y1 + '" x2="' + x2 + '" y2="' + y2 + '" stroke="' + (color || dPal.textMute) + '" stroke-width="1.5" stroke-dasharray="4 3"/>';
}

// ── Tier 2 sub-shape helpers · water/heat/matter ────────────────────────────
function dSun(cx, cy, r) {
  r = r || 14;
  let rays = '';
  for (let i = 0; i < 8; i++) {
    const a = (i * Math.PI) / 4;
    const x1 = cx + Math.cos(a) * (r + 3), y1 = cy + Math.sin(a) * (r + 3);
    const x2 = cx + Math.cos(a) * (r + 9), y2 = cy + Math.sin(a) * (r + 9);
    rays += '<line x1="' + x1.toFixed(1) + '" y1="' + y1.toFixed(1) + '" x2="' + x2.toFixed(1) + '" y2="' + y2.toFixed(1) + '" stroke="' + dPal.orange + '" stroke-width="2"/>';
  }
  return rays + '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="' + dPal.yellow + '" stroke="' + dPal.orange + '" stroke-width="1.5"/>';
}
function dFlame(cx, by, h) {
  h = h || 26;
  const w = h * 0.55;
  const top = by - h, mid = by - h * 0.45;
  return '<path d="M ' + cx + ' ' + top + ' Q ' + (cx + w) + ' ' + mid + ' ' + cx + ' ' + by + ' Q ' + (cx - w) + ' ' + mid + ' ' + cx + ' ' + top + ' Z" fill="' + dPal.orange + '" stroke="' + dPal.red + '" stroke-width="1.2"/>' +
    '<path d="M ' + cx + ' ' + (top + h * 0.3) + ' Q ' + (cx + w * 0.55) + ' ' + (mid + 2) + ' ' + cx + ' ' + (by - 3) + ' Q ' + (cx - w * 0.55) + ' ' + (mid + 2) + ' ' + cx + ' ' + (top + h * 0.3) + ' Z" fill="' + dPal.yellow + '"/>';
}
function dBeaker(x, y, w, h, opts) {
  opts = opts || {};
  const fill = opts.liquid != null ? opts.liquid : 0.6;
  const lh = h * fill, ly = y + h - lh;
  const rim = '<line x1="' + (x - 4) + '" y1="' + y + '" x2="' + (x + w + 4) + '" y2="' + y + '" stroke="' + dPal.textDark + '" stroke-width="2"/>';
  const body = '<path d="M ' + x + ' ' + y + ' L ' + x + ' ' + (y + h) + ' Q ' + x + ' ' + (y + h + 4) + ' ' + (x + 4) + ' ' + (y + h + 4) + ' L ' + (x + w - 4) + ' ' + (y + h + 4) + ' Q ' + (x + w) + ' ' + (y + h + 4) + ' ' + (x + w) + ' ' + (y + h) + ' L ' + (x + w) + ' ' + y + '" fill="white" stroke="' + dPal.textDark + '" stroke-width="1.5"/>';
  const liquid = lh > 0 ? '<rect x="' + (x + 2) + '" y="' + ly + '" width="' + (w - 4) + '" height="' + (lh - 2) + '" fill="' + (opts.color || dPal.blue) + '" opacity="0.55"/>' : '';
  return body + liquid + rim;
}
function dPuddle(cx, cy, rx, ry) {
  rx = rx || 28; ry = ry || 7;
  return '<ellipse cx="' + cx + '" cy="' + cy + '" rx="' + rx + '" ry="' + ry + '" fill="' + dPal.blue + '" opacity="0.6"/>' +
    '<ellipse cx="' + cx + '" cy="' + (cy - 1) + '" rx="' + (rx * 0.6) + '" ry="' + (ry * 0.5) + '" fill="white" opacity="0.4"/>';
}
function dDroplet(cx, cy, r) {
  r = r || 6;
  return '<path d="M ' + cx + ' ' + (cy - r * 1.4) + ' Q ' + (cx + r) + ' ' + cy + ' ' + cx + ' ' + (cy + r) + ' Q ' + (cx - r) + ' ' + cy + ' ' + cx + ' ' + (cy - r * 1.4) + ' Z" fill="' + dPal.blue + '" stroke="' + dPal.blue + '" stroke-width="1"/>';
}
function dCloud(cx, cy, w) {
  w = w || 50;
  const r = w * 0.25;
  return '<g>' +
    '<circle cx="' + (cx - w * 0.3) + '" cy="' + cy + '" r="' + r + '" fill="white" stroke="' + dPal.blue + '" stroke-width="1.5"/>' +
    '<circle cx="' + cx + '" cy="' + (cy - r * 0.5) + '" r="' + (r * 1.1) + '" fill="white" stroke="' + dPal.blue + '" stroke-width="1.5"/>' +
    '<circle cx="' + (cx + w * 0.3) + '" cy="' + cy + '" r="' + r + '" fill="white" stroke="' + dPal.blue + '" stroke-width="1.5"/>' +
    '<rect x="' + (cx - w * 0.4) + '" y="' + (cy - 1) + '" width="' + (w * 0.8) + '" height="' + (r + 2) + '" fill="white"/>' +
  '</g>';
}
function dThermo(x, y, h, fill) {
  h = h || 60;
  const w = 10, br = w * 0.9;
  const fillH = (fill != null ? fill : 0.5) * (h - 8);
  const liqY = y + (h - 8) - fillH;
  return '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + (h - 8) + '" rx="' + (w / 2) + '" fill="white" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
    (fillH > 0 ? '<rect x="' + (x + 2) + '" y="' + liqY + '" width="' + (w - 4) + '" height="' + (fillH - 2) + '" fill="' + dPal.red + '"/>' : '') +
    '<circle cx="' + (x + w / 2) + '" cy="' + (y + h - 4) + '" r="' + br + '" fill="' + dPal.red + '" stroke="' + dPal.textDark + '" stroke-width="1.5"/>';
}
function dContainer(x, y, w, h, opts) {
  opts = opts || {};
  const fill = opts.fill || 'white';
  return '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="4" fill="' + fill + '" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
    (opts.lid ? '<line x1="' + x + '" y1="' + (y + 4) + '" x2="' + (x + w) + '" y2="' + (y + 4) + '" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' : '');
}
function dParticleGrid(x, y, w, h, state) {
  const colors = { solid: dPal.blue, liquid: dPal.teal, gas: dPal.purple };
  const c = colors[state] || dPal.blue;
  let dots = '';
  if (state === 'solid') {
    for (let r = 0; r < 4; r++) for (let cc = 0; cc < 5; cc++) {
      dots += '<circle cx="' + (x + 6 + cc * (w - 12) / 4) + '" cy="' + (y + 6 + r * (h - 12) / 3) + '" r="2.5" fill="' + c + '"/>';
    }
  } else if (state === 'liquid') {
    const positions = [[0.1,0.3],[0.3,0.6],[0.5,0.2],[0.7,0.7],[0.9,0.4],[0.2,0.85],[0.55,0.85],[0.85,0.85],[0.4,0.45],[0.7,0.4]];
    positions.forEach(p => { dots += '<circle cx="' + (x + p[0] * w) + '" cy="' + (y + p[1] * h) + '" r="2.5" fill="' + c + '"/>'; });
  } else {
    const positions = [[0.15,0.2],[0.5,0.15],[0.85,0.3],[0.25,0.55],[0.7,0.5],[0.15,0.8],[0.55,0.8],[0.9,0.75]];
    positions.forEach(p => { dots += '<circle cx="' + (x + p[0] * w) + '" cy="' + (y + p[1] * h) + '" r="2.5" fill="' + c + '"/>'; });
  }
  return '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="4" fill="white" stroke="' + dPal.stroke + '" stroke-width="1"/>' + dots;
}
function dKettle(x, y, w, h, opts) {
  opts = opts || {};
  const sx = x + w * 0.15, sy = y + h * 0.3;
  return '<path d="M ' + (x + 6) + ' ' + (y + h) + ' Q ' + x + ' ' + (y + h * 0.3) + ' ' + (x + w * 0.5) + ' ' + (y + h * 0.15) + ' Q ' + (x + w) + ' ' + (y + h * 0.3) + ' ' + (x + w - 6) + ' ' + (y + h) + ' Z" fill="' + (opts.color || '#C0C8D0') + '" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
    '<line x1="' + (x + w * 0.25) + '" y1="' + (y + h * 0.18) + '" x2="' + (x + w * 0.75) + '" y2="' + (y + h * 0.18) + '" stroke="' + dPal.textDark + '" stroke-width="2"/>' +
    '<path d="M ' + sx + ' ' + sy + ' L ' + (sx - 12) + ' ' + (sy + 4) + ' L ' + (sx - 8) + ' ' + (sy + 14) + '" fill="' + (opts.color || '#C0C8D0') + '" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
    (opts.steam ? '<path d="M ' + (x + w * 0.5) + ' ' + (y + h * 0.05) + ' Q ' + (x + w * 0.65) + ' ' + (y - 5) + ' ' + (x + w * 0.55) + ' ' + (y - 15) + '" stroke="' + dPal.blue + '" stroke-width="2" fill="none" opacity="0.6"/>' : '');
}
function dPlate(x, y, w, h, opts) {
  opts = opts || {};
  return '<ellipse cx="' + (x + w / 2) + '" cy="' + (y + h / 2) + '" rx="' + (w / 2) + '" ry="' + (h / 2) + '" fill="' + (opts.color || 'white') + '" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
    '<ellipse cx="' + (x + w / 2) + '" cy="' + (y + h / 2) + '" rx="' + (w * 0.4) + '" ry="' + (h * 0.4) + '" fill="none" stroke="' + dPal.stroke + '" stroke-width="1"/>';
}
function dCylinder(x, y, w, h, opts) {
  opts = opts || {};
  const fill = opts.liquid != null ? opts.liquid : 0;
  const lh = h * fill;
  return '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="2" fill="white" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
    (lh > 0 ? '<rect x="' + (x + 1) + '" y="' + (y + h - lh) + '" width="' + (w - 2) + '" height="' + (lh - 1) + '" fill="' + (opts.color || dPal.blue) + '" opacity="0.55"/>' : '');
}
function dRays(cx, cy, r, n) {
  n = n || 6;
  let out = '';
  for (let i = 0; i < n; i++) {
    const a = (i * 2 * Math.PI) / n;
    const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
    out += '<line x1="' + cx + '" y1="' + cy + '" x2="' + x.toFixed(1) + '" y2="' + y.toFixed(1) + '" stroke="' + dPal.orange + '" stroke-width="1.5"/>';
  }
  return out;
}
/** dCup: straight-walled glass cross-section, optional water level + outside condensation droplets. */
function dCup(x, y, w, h, opts) {
  opts = opts || {};
  const water = opts.water != null ? opts.water : 0;
  const wh = h * water;
  const wy = y + h - wh;
  let cond = '';
  if (opts.condensation) {
    const rows = [0.25, 0.5, 0.75];
    rows.forEach((r, i) => {
      const cy = y + h * r;
      const sideX = i % 2 === 0 ? x - 3 : x + w + 3;
      cond += '<circle cx="' + sideX + '" cy="' + cy + '" r="2" fill="' + dPal.blue + '" opacity="0.7"/>';
    });
  }
  return '<path d="M ' + x + ' ' + y + ' L ' + x + ' ' + (y + h) + ' L ' + (x + w) + ' ' + (y + h) + ' L ' + (x + w) + ' ' + y + '" fill="white" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
    (wh > 0 ? '<rect x="' + (x + 1) + '" y="' + wy + '" width="' + (w - 2) + '" height="' + (wh - 1) + '" fill="' + (opts.color || dPal.blue) + '" opacity="0.55"/>' : '') +
    cond +
    (opts.label ? '<text x="' + (x + w / 2) + '" y="' + (y + h + 14) + '" text-anchor="middle" font-size="11" fill="' + dPal.textDark + '">' + opts.label + '</text>' : '');
}
/** dFunnel: inverted trapezoid + neck stem. (cx, by) is the bottom-tip of the stem. */
function dFunnel(cx, by, w, h, opts) {
  w = w || 30; h = h || 26;
  opts = opts || {};
  const stemH = h * 0.35;
  const top = by - h;
  const stemW = w * 0.18;
  return '<polygon points="' + (cx - w / 2) + ',' + top + ' ' + (cx + w / 2) + ',' + top + ' ' + (cx + stemW) + ',' + (by - stemH) + ' ' + (cx - stemW) + ',' + (by - stemH) + '" fill="' + (opts.fill || 'white') + '" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
    '<rect x="' + (cx - stemW) + '" y="' + (by - stemH) + '" width="' + (stemW * 2) + '" height="' + stemH + '" fill="' + (opts.fill || 'white') + '" stroke="' + dPal.textDark + '" stroke-width="1.5"/>';
}
/** dBalance: beam balance — pivot triangle, beam, two pans. (cx, cy) is the pivot tip. */
function dBalance(cx, cy, w, opts) {
  w = w || 60;
  opts = opts || {};
  const beamY = cy - 10;
  const tilt = opts.tilt || 0; // -1 left heavy, 1 right heavy, 0 balanced
  const dy = tilt * 6;
  return '<polygon points="' + cx + ',' + cy + ' ' + (cx - 8) + ',' + (cy + 12) + ' ' + (cx + 8) + ',' + (cy + 12) + '" fill="' + dPal.textDark + '"/>' +
    '<line x1="' + (cx - w / 2) + '" y1="' + (beamY + dy) + '" x2="' + (cx + w / 2) + '" y2="' + (beamY - dy) + '" stroke="' + dPal.textDark + '" stroke-width="2"/>' +
    '<line x1="' + (cx - w / 2) + '" y1="' + (beamY + dy) + '" x2="' + (cx - w / 2) + '" y2="' + (beamY + dy + 8) + '" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
    '<line x1="' + (cx + w / 2) + '" y1="' + (beamY - dy) + '" x2="' + (cx + w / 2) + '" y2="' + (beamY - dy + 8) + '" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
    '<path d="M ' + (cx - w / 2 - 8) + ' ' + (beamY + dy + 8) + ' Q ' + (cx - w / 2) + ' ' + (beamY + dy + 14) + ' ' + (cx - w / 2 + 8) + ' ' + (beamY + dy + 8) + '" fill="#C0C8D0" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
    '<path d="M ' + (cx + w / 2 - 8) + ' ' + (beamY - dy + 8) + ' Q ' + (cx + w / 2) + ' ' + (beamY - dy + 14) + ' ' + (cx + w / 2 + 8) + ' ' + (beamY - dy + 8) + '" fill="#C0C8D0" stroke="' + dPal.textDark + '" stroke-width="1"/>';
}
/** dIceCube: rounded white-blue square with diagonal highlight. */
function dIceCube(x, y, w, h) {
  w = w || 24; h = h || 24;
  return '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="3" fill="#D9F0FF" stroke="' + dPal.blue + '" stroke-width="1.5"/>' +
    '<line x1="' + (x + 4) + '" y1="' + (y + h - 4) + '" x2="' + (x + w - 4) + '" y2="' + (y + 4) + '" stroke="white" stroke-width="2" opacity="0.8"/>' +
    '<line x1="' + (x + 4) + '" y1="' + (y + h * 0.4) + '" x2="' + (x + w * 0.4) + '" y2="' + (y + 4) + '" stroke="white" stroke-width="1.5" opacity="0.6"/>';
}
/** dBottle: cylindrical body + neck + cap. */
function dBottle(x, y, w, h, opts) {
  opts = opts || {};
  const neckH = h * 0.18, neckW = w * 0.45, capH = h * 0.08;
  const neckX = x + (w - neckW) / 2;
  const fill = opts.water != null ? opts.water : 0;
  const fillH = (h - neckH - capH) * fill;
  const bodyY = y + neckH + capH;
  const bodyH = h - neckH - capH;
  return '<rect x="' + neckX + '" y="' + y + '" width="' + neckW + '" height="' + capH + '" fill="' + (opts.cap || dPal.red) + '" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
    '<rect x="' + neckX + '" y="' + (y + capH) + '" width="' + neckW + '" height="' + neckH + '" fill="white" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
    '<path d="M ' + x + ' ' + bodyY + ' Q ' + x + ' ' + (bodyY - 4) + ' ' + (x + 4) + ' ' + (bodyY - 4) + ' L ' + (x + w - 4) + ' ' + (bodyY - 4) + ' Q ' + (x + w) + ' ' + (bodyY - 4) + ' ' + (x + w) + ' ' + bodyY + ' L ' + (x + w) + ' ' + (y + h) + ' L ' + x + ' ' + (y + h) + ' Z" fill="white" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
    (fillH > 0 ? '<rect x="' + (x + 1) + '" y="' + (y + h - fillH) + '" width="' + (w - 2) + '" height="' + (fillH - 1) + '" fill="' + (opts.color || dPal.blue) + '" opacity="0.55"/>' : '');
}
/** dTray: shallow flat tray (parallelogram) with shaded edge — defaults to metal grey. */
function dTray(x, y, w, h, opts) {
  opts = opts || {};
  const skew = h * 0.6;
  const fill = opts.fill || '#C0C8D0';
  return '<polygon points="' + x + ',' + (y + h) + ' ' + (x + skew) + ',' + y + ' ' + (x + w) + ',' + y + ' ' + (x + w - skew) + ',' + (y + h) + '" fill="' + fill + '" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
    '<polygon points="' + (x + w - skew) + ',' + (y + h) + ' ' + (x + w) + ',' + y + ' ' + (x + w) + ',' + (y + h * 0.3) + ' ' + (x + w - skew * 0.7) + ',' + (y + h + h * 0.3) + '" fill="#A0A8B0" stroke="' + dPal.textDark + '" stroke-width="1"/>';
}
/** dMist: cluster of 6–10 small light-blue circles inside (cx, cy, w, h) bounding box. */
function dMist(cx, cy, w, h) {
  w = w || 60; h = h || 30;
  const positions = [[0.1,0.4],[0.25,0.7],[0.4,0.3],[0.55,0.6],[0.7,0.4],[0.85,0.65],[0.2,0.2],[0.5,0.85],[0.8,0.2]];
  let dots = '';
  positions.forEach(p => {
    const r = 2 + Math.random() * 1.5;
    dots += '<circle cx="' + (cx - w / 2 + p[0] * w).toFixed(1) + '" cy="' + (cy - h / 2 + p[1] * h).toFixed(1) + '" r="' + r.toFixed(1) + '" fill="' + dPal.blue + '" opacity="0.55"/>';
  });
  return dots;
}
/** dSpring: horizontal coil zig-zag for compression diagrams. */
function dSpring(x, y, w, h, opts) {
  opts = opts || {};
  const coils = opts.coils || 6;
  const dx = w / coils;
  const cy = y + h / 2;
  let path = 'M ' + x + ' ' + cy;
  for (let i = 0; i < coils; i++) {
    path += ' L ' + (x + dx * (i + 0.5)) + ' ' + (i % 2 === 0 ? y : y + h);
    path += ' L ' + (x + dx * (i + 1)) + ' ' + cy;
  }
  return '<path d="' + path + '" fill="none" stroke="' + (opts.color || dPal.textDark) + '" stroke-width="2" stroke-linejoin="round"/>';
}

// ── Tier 2 sub-shape helpers · biology ──────────────────────────────────────
function dPlant(x, y, h, opts) {
  h = h || 60;
  opts = opts || {};
  const cx = x;
  return '<line x1="' + cx + '" y1="' + (y + h) + '" x2="' + cx + '" y2="' + y + '" stroke="' + (opts.stem || '#3FA34D') + '" stroke-width="2"/>' +
    dLeaf(cx - 6, y + h * 0.45, 14, 7, { rot: -25, color: opts.leaf }) +
    dLeaf(cx + 6, y + h * 0.6, 14, 7, { rot: 25, color: opts.leaf }) +
    (opts.flower ? dFlower(cx, y, 6, { color: opts.flower }) : '') +
    '<line x1="' + (cx - 5) + '" y1="' + (y + h) + '" x2="' + (cx - 8) + '" y2="' + (y + h + 6) + '" stroke="#7B4F2C" stroke-width="1.5"/>' +
    '<line x1="' + (cx + 5) + '" y1="' + (y + h) + '" x2="' + (cx + 8) + '" y2="' + (y + h + 6) + '" stroke="#7B4F2C" stroke-width="1.5"/>';
}
function dFlower(cx, cy, r, opts) {
  r = r || 7;
  opts = opts || {};
  const c = opts.color || '#FF8FB3';
  let petals = '';
  for (let i = 0; i < 5; i++) {
    const a = (i * 2 * Math.PI) / 5 - Math.PI / 2;
    const px = cx + Math.cos(a) * r, py = cy + Math.sin(a) * r;
    petals += '<circle cx="' + px.toFixed(1) + '" cy="' + py.toFixed(1) + '" r="' + (r * 0.6).toFixed(1) + '" fill="' + c + '"/>';
  }
  return petals + '<circle cx="' + cx + '" cy="' + cy + '" r="' + (r * 0.45) + '" fill="' + dPal.yellow + '"/>';
}
function dLeaf(cx, cy, w, h, opts) {
  w = w || 14; h = h || 7;
  opts = opts || {};
  const rot = opts.rot || 0;
  return '<g transform="translate(' + cx + ',' + cy + ') rotate(' + rot + ')">' +
    '<path d="M 0 0 Q ' + (w / 2) + ' ' + (-h) + ' ' + w + ' 0 Q ' + (w / 2) + ' ' + h + ' 0 0 Z" fill="' + (opts.color || '#5BB85B') + '" stroke="#3FA34D" stroke-width="1"/>' +
  '</g>';
}
// source: bioicons-style primary-school animal silhouettes (CC0-equivalent)
// All silhouettes occupy a ~28×16 box anchored at (x, y) where y is body-center
function dAnimal(x, y, kind) {
  if (kind === 'fish') {
    // streamlined body + crescent tail + gill line + eye dot
    return '<path d="M ' + x + ' ' + y + ' C ' + (x + 4) + ' ' + (y - 9) + ' ' + (x + 22) + ' ' + (y - 9) + ' ' + (x + 28) + ' ' + y + ' C ' + (x + 22) + ' ' + (y + 9) + ' ' + (x + 4) + ' ' + (y + 9) + ' ' + x + ' ' + y + ' Z" fill="' + dPal.blue + '" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
      '<path d="M ' + x + ' ' + y + ' L ' + (x - 7) + ' ' + (y - 7) + ' L ' + (x - 4) + ' ' + y + ' L ' + (x - 7) + ' ' + (y + 7) + ' Z" fill="' + dPal.blue + '" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
      '<path d="M ' + (x + 7) + ' ' + (y - 6) + ' Q ' + (x + 9) + ' ' + y + ' ' + (x + 7) + ' ' + (y + 6) + '" fill="none" stroke="' + dPal.textDark + '" stroke-width="0.8" opacity="0.5"/>' +
      '<circle cx="' + (x + 22) + '" cy="' + (y - 2) + '" r="1.4" fill="white" stroke="' + dPal.textDark + '" stroke-width="0.5"/>' +
      '<circle cx="' + (x + 22) + '" cy="' + (y - 2) + '" r="0.6" fill="' + dPal.textDark + '"/>';
  }
  if (kind === 'bird') {
    // round body + small head + triangular beak + folded wing arc + eye
    return '<ellipse cx="' + (x + 10) + '" cy="' + (y + 1) + '" rx="11" ry="7" fill="' + dPal.orange + '" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
      '<circle cx="' + (x + 21) + '" cy="' + (y - 5) + '" r="5" fill="' + dPal.orange + '" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
      '<polygon points="' + (x + 25) + ',' + (y - 6) + ' ' + (x + 32) + ',' + (y - 4) + ' ' + (x + 25) + ',' + (y - 2) + '" fill="' + dPal.yellow + '" stroke="' + dPal.textDark + '" stroke-width="0.6"/>' +
      '<path d="M ' + (x + 4) + ' ' + (y - 1) + ' Q ' + (x + 10) + ' ' + (y - 6) + ' ' + (x + 16) + ' ' + (y - 1) + '" fill="none" stroke="' + dPal.textDark + '" stroke-width="1" opacity="0.6"/>' +
      '<line x1="' + (x + 8) + '" y1="' + (y + 8) + '" x2="' + (x + 7) + '" y2="' + (y + 12) + '" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
      '<line x1="' + (x + 13) + '" y1="' + (y + 8) + '" x2="' + (x + 14) + '" y2="' + (y + 12) + '" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
      '<circle cx="' + (x + 22) + '" cy="' + (y - 6) + '" r="0.9" fill="' + dPal.textDark + '"/>';
  }
  if (kind === 'insect') {
    // ladybug: round body + head + spots + 6 legs + antennae
    return '<line x1="' + (x + 4) + '" y1="' + (y - 1) + '" x2="' + (x + 1) + '" y2="' + (y - 7) + '" stroke="' + dPal.textDark + '" stroke-width="0.8"/>' +
      '<line x1="' + (x + 8) + '" y1="' + (y - 1) + '" x2="' + (x + 11) + '" y2="' + (y - 7) + '" stroke="' + dPal.textDark + '" stroke-width="0.8"/>' +
      '<circle cx="' + (x + 6) + '" cy="' + (y + 1) + '" r="3" fill="' + dPal.textDark + '"/>' +
      '<ellipse cx="' + (x + 12) + '" cy="' + (y + 2) + '" rx="7" ry="6" fill="' + dPal.red + '" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
      '<line x1="' + (x + 12) + '" y1="' + (y - 4) + '" x2="' + (x + 12) + '" y2="' + (y + 8) + '" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
      '<circle cx="' + (x + 9) + '" cy="' + y + '" r="0.9" fill="' + dPal.textDark + '"/>' +
      '<circle cx="' + (x + 15) + '" cy="' + y + '" r="0.9" fill="' + dPal.textDark + '"/>' +
      '<circle cx="' + (x + 9) + '" cy="' + (y + 5) + '" r="0.9" fill="' + dPal.textDark + '"/>' +
      '<circle cx="' + (x + 15) + '" cy="' + (y + 5) + '" r="0.9" fill="' + dPal.textDark + '"/>' +
      '<line x1="' + (x + 5) + '" y1="' + (y + 2) + '" x2="' + (x + 1) + '" y2="' + (y + 5) + '" stroke="' + dPal.textDark + '" stroke-width="0.8"/>' +
      '<line x1="' + (x + 19) + '" y1="' + (y + 2) + '" x2="' + (x + 23) + '" y2="' + (y + 5) + '" stroke="' + dPal.textDark + '" stroke-width="0.8"/>';
  }
  // mammal: dog-like quadruped with body + head + ears + 4 legs + tail
  return '<ellipse cx="' + (x + 13) + '" cy="' + (y + 1) + '" rx="11" ry="5" fill="#A87B5C" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
    '<circle cx="' + (x + 24) + '" cy="' + (y - 4) + '" r="4.5" fill="#A87B5C" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
    '<polygon points="' + (x + 21) + ',' + (y - 8) + ' ' + (x + 23) + ',' + (y - 11) + ' ' + (x + 24) + ',' + (y - 7) + '" fill="#A87B5C" stroke="' + dPal.textDark + '" stroke-width="0.8"/>' +
    '<polygon points="' + (x + 26) + ',' + (y - 8) + ' ' + (x + 28) + ',' + (y - 11) + ' ' + (x + 27) + ',' + (y - 6) + '" fill="#A87B5C" stroke="' + dPal.textDark + '" stroke-width="0.8"/>' +
    '<circle cx="' + (x + 25) + '" cy="' + (y - 4) + '" r="0.7" fill="' + dPal.textDark + '"/>' +
    '<ellipse cx="' + (x + 27) + '" cy="' + (y - 1) + '" rx="1.2" ry="0.8" fill="' + dPal.textDark + '"/>' +
    '<rect x="' + (x + 3) + '" y="' + (y + 5) + '" width="2" height="6" fill="#A87B5C" stroke="' + dPal.textDark + '" stroke-width="0.8"/>' +
    '<rect x="' + (x + 9) + '" y="' + (y + 5) + '" width="2" height="6" fill="#A87B5C" stroke="' + dPal.textDark + '" stroke-width="0.8"/>' +
    '<rect x="' + (x + 16) + '" y="' + (y + 5) + '" width="2" height="6" fill="#A87B5C" stroke="' + dPal.textDark + '" stroke-width="0.8"/>' +
    '<rect x="' + (x + 21) + '" y="' + (y + 5) + '" width="2" height="6" fill="#A87B5C" stroke="' + dPal.textDark + '" stroke-width="0.8"/>' +
    '<path d="M ' + (x + 2) + ' ' + (y - 1) + ' Q ' + (x - 4) + ' ' + (y - 4) + ' ' + (x - 5) + ' ' + (y + 2) + '" fill="none" stroke="#A87B5C" stroke-width="2.5" stroke-linecap="round"/>';
}
// source: bioicons-style primary-school anatomical organs (CC0-equivalent)
// Each kind occupies a ~28×30 box anchored at top-left (x, y)
function dOrgan(x, y, kind) {
  if (kind === 'heart') {
    // Classic two-lobe valentine with apex ventricle + aorta stub
    return '<path d="M ' + (x + 14) + ' ' + (y + 28) + ' C ' + (x - 2) + ' ' + (y + 16) + ' ' + (x - 2) + ' ' + (y + 4) + ' ' + (x + 8) + ' ' + (y + 4) + ' C ' + (x + 12) + ' ' + (y + 4) + ' ' + (x + 14) + ' ' + (y + 7) + ' ' + (x + 14) + ' ' + (y + 10) + ' C ' + (x + 14) + ' ' + (y + 7) + ' ' + (x + 16) + ' ' + (y + 4) + ' ' + (x + 20) + ' ' + (y + 4) + ' C ' + (x + 30) + ' ' + (y + 4) + ' ' + (x + 30) + ' ' + (y + 16) + ' ' + (x + 14) + ' ' + (y + 28) + ' Z" fill="' + dPal.red + '" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
      '<path d="M ' + (x + 12) + ' ' + (y + 5) + ' Q ' + (x + 14) + ' ' + y + ' ' + (x + 17) + ' ' + (y + 2) + '" fill="none" stroke="' + dPal.red + '" stroke-width="2.5" stroke-linecap="round"/>' +
      '<path d="M ' + (x + 8) + ' ' + (y + 12) + ' Q ' + (x + 14) + ' ' + (y + 18) + ' ' + (x + 20) + ' ' + (y + 12) + '" fill="none" stroke="' + dPal.textDark + '" stroke-width="0.6" opacity="0.4"/>';
  }
  if (kind === 'lung') {
    // Two lobed lungs with trachea + bifurcation; rib indent on outer edges
    return '<line x1="' + (x + 14) + '" y1="' + y + '" x2="' + (x + 14) + '" y2="' + (y + 8) + '" stroke="' + dPal.textDark + '" stroke-width="2"/>' +
      '<line x1="' + (x + 14) + '" y1="' + (y + 8) + '" x2="' + (x + 9) + '" y2="' + (y + 12) + '" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
      '<line x1="' + (x + 14) + '" y1="' + (y + 8) + '" x2="' + (x + 19) + '" y2="' + (y + 12) + '" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
      '<path d="M ' + (x + 9) + ' ' + (y + 12) + ' C ' + (x + 1) + ' ' + (y + 14) + ' ' + (x - 1) + ' ' + (y + 24) + ' ' + (x + 4) + ' ' + (y + 28) + ' C ' + (x + 9) + ' ' + (y + 30) + ' ' + (x + 12) + ' ' + (y + 24) + ' ' + (x + 12) + ' ' + (y + 16) + ' Z" fill="#FFB3B3" stroke="' + dPal.red + '" stroke-width="1"/>' +
      '<path d="M ' + (x + 19) + ' ' + (y + 12) + ' C ' + (x + 27) + ' ' + (y + 14) + ' ' + (x + 29) + ' ' + (y + 24) + ' ' + (x + 24) + ' ' + (y + 28) + ' C ' + (x + 19) + ' ' + (y + 30) + ' ' + (x + 16) + ' ' + (y + 24) + ' ' + (x + 16) + ' ' + (y + 16) + ' Z" fill="#FFB3B3" stroke="' + dPal.red + '" stroke-width="1"/>' +
      '<path d="M ' + (x + 5) + ' ' + (y + 18) + ' Q ' + (x + 8) + ' ' + (y + 20) + ' ' + (x + 11) + ' ' + (y + 18) + '" fill="none" stroke="' + dPal.red + '" stroke-width="0.6" opacity="0.6"/>' +
      '<path d="M ' + (x + 17) + ' ' + (y + 18) + ' Q ' + (x + 20) + ' ' + (y + 20) + ' ' + (x + 23) + ' ' + (y + 18) + '" fill="none" stroke="' + dPal.red + '" stroke-width="0.6" opacity="0.6"/>';
  }
  if (kind === 'stomach') {
    // J-shape with esophagus stub at top-left + duodenum stub at right
    return '<line x1="' + (x + 6) + '" y1="' + y + '" x2="' + (x + 6) + '" y2="' + (y + 6) + '" stroke="#E29A60" stroke-width="3" stroke-linecap="round"/>' +
      '<path d="M ' + (x + 4) + ' ' + (y + 6) + ' C ' + (x + 4) + ' ' + (y + 4) + ' ' + (x + 22) + ' ' + (y + 6) + ' ' + (x + 24) + ' ' + (y + 14) + ' C ' + (x + 26) + ' ' + (y + 22) + ' ' + (x + 18) + ' ' + (y + 28) + ' ' + (x + 12) + ' ' + (y + 26) + ' C ' + (x + 4) + ' ' + (y + 24) + ' ' + (x + 1) + ' ' + (y + 16) + ' ' + (x + 4) + ' ' + (y + 6) + ' Z" fill="#FFCFA0" stroke="' + dPal.orange + '" stroke-width="1"/>' +
      '<path d="M ' + (x + 24) + ' ' + (y + 14) + ' Q ' + (x + 28) + ' ' + (y + 14) + ' ' + (x + 28) + ' ' + (y + 20) + '" fill="none" stroke="#E29A60" stroke-width="3" stroke-linecap="round"/>' +
      '<path d="M ' + (x + 7) + ' ' + (y + 14) + ' Q ' + (x + 14) + ' ' + (y + 12) + ' ' + (x + 21) + ' ' + (y + 14) + '" fill="none" stroke="' + dPal.orange + '" stroke-width="0.6" opacity="0.5"/>';
  }
  if (kind === 'kidney') {
    // Classic kidney bean with renal pelvis indent on the inside
    return '<path d="M ' + (x + 18) + ' ' + (y + 4) + ' C ' + (x + 28) + ' ' + (y + 4) + ' ' + (x + 28) + ' ' + (y + 26) + ' ' + (x + 18) + ' ' + (y + 26) + ' C ' + (x + 14) + ' ' + (y + 26) + ' ' + (x + 12) + ' ' + (y + 22) + ' ' + (x + 12) + ' ' + (y + 18) + ' C ' + (x + 12) + ' ' + (y + 16) + ' ' + (x + 8) + ' ' + (y + 16) + ' ' + (x + 6) + ' ' + (y + 14) + ' C ' + (x + 4) + ' ' + (y + 12) + ' ' + (x + 6) + ' ' + (y + 8) + ' ' + (x + 10) + ' ' + (y + 6) + ' C ' + (x + 13) + ' ' + (y + 4) + ' ' + (x + 16) + ' ' + (y + 4) + ' ' + (x + 18) + ' ' + (y + 4) + ' Z" fill="#B86552" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
      '<circle cx="' + (x + 10) + '" cy="' + (y + 13) + '" r="1.5" fill="#7A3F30" opacity="0.7"/>';
  }
  return '';
}
function dSeed(cx, cy, r) {
  r = r || 5;
  return '<ellipse cx="' + cx + '" cy="' + cy + '" rx="' + r + '" ry="' + (r * 1.4) + '" fill="#7B4F2C" stroke="' + dPal.textDark + '" stroke-width="1"/>';
}
function dRoot(x, y, h, opts) {
  h = h || 20;
  opts = opts || {};
  const c = opts.color || '#7B4F2C';
  return '<line x1="' + x + '" y1="' + y + '" x2="' + x + '" y2="' + (y + h) + '" stroke="' + c + '" stroke-width="1.5"/>' +
    '<line x1="' + x + '" y1="' + (y + h * 0.6) + '" x2="' + (x - h * 0.4) + '" y2="' + (y + h) + '" stroke="' + c + '" stroke-width="1"/>' +
    '<line x1="' + x + '" y1="' + (y + h * 0.6) + '" x2="' + (x + h * 0.4) + '" y2="' + (y + h) + '" stroke="' + c + '" stroke-width="1"/>';
}

// ── Tier 2 sub-shape helpers · electricity ──────────────────────────────────
function dBattery(x, y, w, h) {
  w = w || 36; h = h || 16;
  return '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="2" fill="' + dPal.yellow + '" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
    '<rect x="' + (x + w) + '" y="' + (y + h * 0.3) + '" width="3" height="' + (h * 0.4) + '" fill="' + dPal.textDark + '"/>' +
    '<text x="' + (x + 5) + '" y="' + (y + h - 5) + '" font-size="9" font-weight="700" fill="' + dPal.textDark + '">+</text>' +
    '<text x="' + (x + w - 9) + '" y="' + (y + h - 5) + '" font-size="9" font-weight="700" fill="' + dPal.textDark + '">−</text>';
}
function dBulb(cx, cy, r, opts) {
  r = r || 12;
  opts = opts || {};
  const lit = opts.lit !== false;
  return (lit ? dRays(cx, cy, r + 6, 8) : '') +
    '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="' + (lit ? dPal.yellow : '#E8E8E8') + '" stroke="' + dPal.textDark + '" stroke-width="1.5"/>' +
    '<rect x="' + (cx - 4) + '" y="' + (cy + r - 2) + '" width="8" height="5" fill="#999" stroke="' + dPal.textDark + '" stroke-width="1"/>' +
    '<line x1="' + (cx - 3) + '" y1="' + (cy + r + 4) + '" x2="' + (cx + 3) + '" y2="' + (cy + r + 4) + '" stroke="' + dPal.textDark + '" stroke-width="1"/>';
}
function dSwitch(x, y, w, opts) {
  w = w || 28;
  opts = opts || {};
  const open = opts.open !== false;
  const x2 = x + w, y2 = open ? y - 8 : y;
  return '<circle cx="' + x + '" cy="' + y + '" r="2.5" fill="' + dPal.textDark + '"/>' +
    '<circle cx="' + (x + w) + '" cy="' + y + '" r="2.5" fill="' + dPal.textDark + '"/>' +
    '<line x1="' + x + '" y1="' + y + '" x2="' + x2 + '" y2="' + y2 + '" stroke="' + dPal.textDark + '" stroke-width="2"/>';
}
function dWire(x1, y1, x2, y2, opts) {
  opts = opts || {};
  return '<line x1="' + x1 + '" y1="' + y1 + '" x2="' + x2 + '" y2="' + y2 + '" stroke="' + (opts.color || dPal.textDark) + '" stroke-width="' + (opts.sw || 2) + '"/>';
}
function dCircuit(id, components, wires, opts) {
  opts = opts || {};
  const w = opts.w || 500, h = opts.h || 200;
  const body = (components || []).join('') + (wires || []).join('');
  const svg =
    '<svg viewBox="0 0 ' + w + ' ' + h + '" width="100%" style="max-width:100%;height:auto;display:block;margin:0 auto">' +
      _D_DEFS +
      '<rect x="0" y="0" width="' + w + '" height="' + h + '" rx="6" fill="' + (opts.bg || '#FAFBFF') + '"/>' +
      body +
    '</svg>';
  _dMount(id, svg);
}

// ── Tier 3 ECharts helpers ───────────────────────────────────────────────────
// Default tooltip + palette + resize listener. Replace inline echarts.init
// + setOption boilerplate.
const _ecPal = ['#4D96FF', '#4ECDC4', '#FFE066', '#FF6B6B', '#B388FF', '#2ECC71'];
const _ecGrid = { left: '10%', right: '5%', bottom: '15%', top: '40px', containLabel: true };

function _ecTitle(text) { return { text: text, textStyle: { fontSize: 13, color: dPal.textDark } }; }
function _ecInit(id, opt) {
  const el = document.getElementById(id);
  if (!el) { console.warn('[ec-helper] missing #' + id); return; }
  const c = echarts.init(el);
  c.setOption(opt);
  window.addEventListener('resize', function() { c.resize(); });
}

// ecBar(id, title, xLabels, series, opts?) — series may be a number array
// (single bar) or {name, data, color?} object array (grouped/stacked).
function ecBar(id, title, xLabels, series, opts) {
  const o = opts || {};
  const norm = Array.isArray(series) && typeof series[0] === 'number'
    ? [{ type: 'bar', data: series, itemStyle: { color: _ecPal[0] } }]
    : (series || []).map((s, i) => ({
        name: s.name, type: 'bar', data: s.data,
        itemStyle: { color: s.color || _ecPal[i % _ecPal.length] }
      }));
  _ecInit(id, {
    title: _ecTitle(title), tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: xLabels, axisLabel: { fontSize: 11 } },
    yAxis: { type: 'value', name: o.yName || '', nameTextStyle: { fontSize: 11 } },
    series: norm, grid: _ecGrid
  });
}

// ecLine(id, title, xLabels, series, opts?) — series same shape as ecBar.
function ecLine(id, title, xLabels, series, opts) {
  const o = opts || {};
  const norm = Array.isArray(series) && typeof series[0] === 'number'
    ? [{ type: 'line', data: series, itemStyle: { color: _ecPal[0] }, smooth: true }]
    : (series || []).map((s, i) => ({
        name: s.name, type: 'line', data: s.data, smooth: true,
        itemStyle: { color: s.color || _ecPal[i % _ecPal.length] }
      }));
  _ecInit(id, {
    title: _ecTitle(title), tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: xLabels, axisLabel: { fontSize: 11 } },
    yAxis: { type: 'value', name: o.yName || '', nameTextStyle: { fontSize: 11 } },
    series: norm, grid: _ecGrid
  });
}

// ecPie(id, title, items) — items = [{name, value}, ...].
function ecPie(id, title, items) {
  _ecInit(id, {
    title: _ecTitle(title), tooltip: { trigger: 'item' },
    color: _ecPal,
    series: [{ type: 'pie', radius: ['40%', '70%'], data: items,
      label: { fontSize: 11 } }]
  });
}

window.addEventListener('DOMContentLoaded', function() {
  initNavigation();
  updateAllProgress();
  showTab('overview');
  // Flip cards — delegated handler, no onclick/id needed on the card element
  document.addEventListener('click', function(e) {
    if (e.target.closest('button, a, input, label')) return;
    const card = e.target.closest('.flip-card');
    if (card) card.classList.toggle('flipped');
  });
});
