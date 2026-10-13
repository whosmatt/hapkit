import { View, TimePlot, SpectroPlot, SpectrumPlot } from './plot.js';

const $ = (id) => document.getElementById(id);
const clone = (o) => JSON.parse(JSON.stringify(o));
const fmt = (v, d = 2) => (v == null || Number.isNaN(v) ? '–' : Number(v).toFixed(d));

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'class') e.className = v;
    else if (k === 'value') e.value = v;
    else if (k === 'checked') e.checked = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}

async function api(method, path, body) {
  const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

const S = {
  project: null, path: null, dirty: false, issues: [],
  sel: null, seg: null, render: null, undo: [], redo: [],
  route: 'ram', gain: 128, logSeq: 0, dev: { connected: false },
};

// ---------------------------------------------------------------- log
function log(text, level = 'info') {
  const line = el('div', { class: level }, `${new Date().toTimeString().slice(0, 8)} ${text}`);
  const box = $('log'); box.append(line);
  while (box.childNodes.length > 600) box.firstChild.remove();
  box.scrollTop = box.scrollHeight;
}
async function pollLog() {
  try {
    const r = await api('GET', `/api/device/log?since=${S.logSeq}`);
    for (const ln of r.lines) log(ln.text, ln.level === 'info' ? 'dev' : ln.level);
    S.logSeq = r.next;
  } catch { /* server gone */ }
  setTimeout(pollLog, 1000);
}

// ---------------------------------------------------------------- project / edits
const coll = (name) => S.project[name] || [];
const byId = (name, id) => coll(name).find((x) => x.id === id);
const waveIds = () => coll('waves').map((w) => w.id);

function setProject(p, { path, dirty } = {}) {
  S.project = p;
  if (path !== undefined) S.path = path;
  if (dirty !== undefined) S.dirty = dirty;
  if (!S.sel || !byId(S.sel.coll, S.sel.id)) S.sel = defaultSel();
  refreshAll();
  scheduleLens();
}

function defaultSel() {
  const e = coll('effects').find((x) => x.enabled);
  if (e) return { coll: 'effects', id: e.id };
  if (coll('waves').length) return { coll: 'waves', id: coll('waves')[0].id };
  return null;
}

function edit(fn, opts = {}) {
  if (!opts.noUndo) { S.undo.push(clone(S.project)); if (S.undo.length > 300) S.undo.shift(); S.redo = []; }
  fn(S.project);
  S.dirty = true;
  if (!opts.keepInspector) renderInspector();
  renderLibrary(); renderTop();
  requestRender(true);
  scheduleLens();
}

function undo() { if (!S.undo.length) return; S.redo.push(clone(S.project)); S.project = S.undo.pop(); S.dirty = true; afterHistory(); }
function redo() { if (!S.redo.length) return; S.undo.push(clone(S.project)); S.project = S.redo.pop(); S.dirty = true; afterHistory(); }
function afterHistory() { if (!S.sel || !byId(S.sel.coll, S.sel.id)) S.sel = defaultSel(); refreshAll(); requestRender(true); scheduleLens(); }

// ---------------------------------------------------------------- render loop (latest wins)
let busy = false, again = false, needProject = false, timer = 0;
function requestRender(withProject = false) {
  needProject = needProject || withProject;
  clearTimeout(timer); timer = setTimeout(doRender, 30);
}
function target() {
  if (!S.sel) return null;
  const type = { waves: 'wave', sequences: 'sequence', effects: 'effect', references: 'reference' }[S.sel.coll];
  return { type, id: S.sel.id, route: S.route === 'rtp' ? 'stream' : 'ram' };
}
function overlaySpec() {
  const ref = $('ov_ref').value; if (!ref) return null;
  return { ref, offset_ms: +$('ov_off').value || 0, gain: +$('ov_gain').value || 1, retarget: $('ov_mode').value, f_ref: +$('ov_fref').value || 0 };
}
function renderOptions() {
  return { spec: { win: +$('v_win').value, hop: 8, nfft: 2048, fmax: +$('v_fmax').value || 800, db_range: +$('v_db').value || 60 },
    sim: { enabled: $('v_sim').checked, tail_ms: +$('v_tail').value || 60, robustness: $('v_rob').checked } };
}
async function doRender() {
  if (busy) { again = true; return; }
  const t = target();
  if (!t) { S.render = null; drawPlots(true); return; }
  busy = true; again = false;
  const body = { target: t, overlay: overlaySpec(), options: renderOptions() };
  if (needProject) { body.project = S.project; needProject = false; }
  const selKey = `${t.type}:${t.id}`;
  try {
    const r = await api('POST', '/api/render', body);
    const fit = S.lastKey !== selKey; S.lastKey = selKey;
    S.render = r; S.renderErr = null; drawPlots(fit);
  } catch (e) { S.renderErr = e.message; renderMetrics(); log(`render: ${e.message}`, 'error'); }
  busy = false;
  if (again) doRender();
}

// ---------------------------------------------------------------- plots
const view = new View();
const wav = new TimePlot($('wavCanvas'), view, { hover: onHover, handleDrag: onHandleDrag });
const sgp = new SpectroPlot($('sgCanvas'), view, { hover: onSgHover });
const spp = new SpectrumPlot($('specCanvas'));

function drawPlots(fit) {
  const r = S.render, dev = S.project.device;
  if (!r) { wav.setData(null); sgp.setData(null); spp.setData(null); renderMetrics(); return; }
  const n = Math.max(r.samples.length, r.sim?.n || 0);
  const ovMs = r.overlay ? r.overlay.float.length * 1000 / r.overlay.fs + (r.overlay.offset_ms || 0) : 0;
  view.tmax = Math.max(1, n * 1000 / r.fs, ovMs);
  wav.setData({ ...r });
  wav.show = { env: $('v_env').checked, sim: $('v_sim').checked };
  sgp.setData(r.spectrogram, r.ifreq, dev.f0, +$('v_fmax').value || 800);
  spp.setData(r.spectrum, dev.f0, dev.q_drive, +$('v_fmax').value || 800);
  wav.setHandles(computeHandles());
  if (fit) view.fit(); else view.emit();
  renderMetrics();
}

function renderMetrics() {
  const r = S.render, m = $('metrics'); m.replaceChildren();
  const add = (k, v, cls) => m.append(el('span', { class: cls }, `${k} `, el('b', {}, v)));
  if (S.renderErr) { add('error', S.renderErr, 'err'); return; }
  if (!r) return;
  add('dur', `${fmt(r.duration_ms)} ms`);
  if (r.kernel_ms != null) add('kernel', `${r.kernel_ms} ms`);
  if (r.stream_ms != null) add('hal', `${r.stream_ms} ms`);
  add('n', r.samples.length);
  if (r.native_fs) add('fs₀', r.native_fs);
  if (r.ram) add('ram', `#${r.ram.index} ${r.ram.bytes} B · ${r.ram.used}/${r.ram.capacity}`, r.ram.fits ? '' : 'err');
  if (r.clips) add('clips', r.clips, 'warn');
  const s = r.sim?.metrics;
  if (s) { add('peak', fmt(s.peak_acc, 3)); add('rise', `${fmt(s.rise_ms, 1)} ms`); add('ring', `${fmt(s.ring_ms, 1)} ms`); add('resid', fmt(s.residual_ratio, 4)); }
  for (const w of r.warnings || []) add('⚠', w, 'warn');
  renderSide();
}

function renderSide() {
  const box = $('sidePanel'); box.replaceChildren(); const r = S.render;
  const lg = (color, label) => el('div', {}, el('span', { class: 'lg', style: `background:${color}` }), label);
  box.append(lg('var(--accent)', 'drive (int8)'), lg('var(--env)', 'envelope'), lg('var(--sim)', `LRA acc · Qd ${S.project.device.q_drive} Qf ${S.project.device.q_free}`),
    lg('var(--ref)', 'overlay'), lg('#4cc3ff', 'inst. f (half cycles)'));
  if (!r) return;
  const f = (r.ifreq || []).map((p) => p[1]);
  if (f.length) {
    f.sort((a, b) => a - b);
    box.append(el('div', {}, 'f ', el('b', {}, `${fmt(f[Math.floor(f.length / 2)], 1)}`), ` Hz med · ${fmt(f[0], 0)}–${fmt(f[f.length - 1], 0)}`));
  }
  if (r.sim?.robustness) {
    box.append(el('table', { class: 'mt' }, el('tr', {}, el('th', {}, 'f0'), el('th', {}, 'peak'), el('th', {}, 'rise'), el('th', {}, 'ring')),
      r.sim.robustness.map((x) => el('tr', {}, el('td', {}, `${x.rel > 0 ? '+' : ''}${Math.round(x.rel * 100)}%`), el('td', {}, fmt(x.peak_acc, 3)),
        el('td', {}, fmt(x.rise_ms, 1)), el('td', {}, fmt(x.ring_ms, 1))))));
  }
}

function onHover(h) {
  const ro = $('readout'); const r = S.render;
  if (!h || !r) { ro.style.display = 'none'; return; }
  const i = Math.round(h.t * r.fs / 1000);
  const lines = [`t ${h.t.toFixed(3)} ms  i ${i}`];
  if (i >= 0 && i < r.samples.length) lines.push(`s ${r.samples[i]}  env ${fmt(r.env?.[i] * 127, 1)}`);
  if (r.sim?.acc && i >= 0 && i < r.sim.acc.length) lines.push(`acc ${fmt(r.sim.acc[i], 3)}`);
  const f = r.ifreq?.reduce((best, p) => (Math.abs(p[0] - h.t) < Math.abs((best?.[0] ?? 1e9) - h.t) ? p : best), null);
  if (f && Math.abs(f[0] - h.t) < 5) lines.push(`f ${fmt(f[1], 1)} Hz`);
  ro.textContent = lines.join('\n'); ro.style.display = 'block';
}
function onSgHover(h) {
  const ro = $('readout');
  if (!h) return;
  ro.textContent = `t ${h.t.toFixed(2)} ms\nf ${h.f.toFixed(1)} Hz${h.db != null ? `\n${h.db.toFixed(1)} dB` : ''}`;
  ro.style.display = 'block';
}

// ---------------------------------------------------------------- direct manipulation
function computeHandles() {
  const r = S.render; if (!r || S.sel?.coll !== 'waves') return [];
  const w = byId('waves', S.sel.id); if (!w) return [];
  const gain = w.post?.gain ?? 1; const hs = [];
  for (const b of r.bounds || []) {
    const seg = w.segments[b.index]; if (!seg) continue;
    const t0 = b.start * 1000 / r.fs, t1 = b.end * 1000 / r.fs;
    if (seg.type === 'tone') {
      (seg.amp?.points || []).forEach((p, k) => hs.push({ kind: 'amp', seg: b.index, pt: k, t: t0 + p[0] * (t1 - t0), v: p[1] * gain, t0, t1 }));
    }
    if ((seg.type === 'tone' || seg.type === 'gap') && seg.length) hs.push({ kind: 'end', seg: b.index, t: t1, v: 0, t0, t1 });
  }
  return hs;
}

let dragSnap = null;
function onHandleDrag(h, cur, e, done) {
  const w = byId('waves', S.sel?.id); if (!w) return;
  if (!dragSnap) dragSnap = clone(S.project);
  const seg = w.segments[h.seg]; const gain = w.post?.gain ?? 1;
  if (h.kind === 'amp') {
    seg.amp.points[h.pt][1] = Math.round((cur.v / gain) * 1000) / 1000;
  } else {
    const ms = Math.max(1000 / S.project.device.fs, cur.t - h.t0);
    const L = seg.length;
    if (L.unit === 'ms') L.value = Math.round(ms * 100) / 100;
    else if (L.unit === 'samples') L.value = Math.max(1, Math.round(ms * S.project.device.fs / 1000));
    else {
      const f = seg.type === 'gap' ? S.project.device.f0 : meanFreq(seg);
      let c = ms * f / 1000; c = e.shiftKey ? Math.max(0.5, Math.round(c * 2) / 2) : Math.max(0.05, Math.round(c * 100) / 100);
      L.value = c;
    }
  }
  S.seg = h.seg;
  if (done) {
    S.undo.push(dragSnap); S.redo = []; dragSnap = null; S.dirty = true;
    renderInspector(); renderLibrary(); renderTop();
  }
  requestRender(true);
}
function meanFreq(seg) {
  const pts = seg.freq?.points || [[0, 1], [1, 1]];
  const m = pts.reduce((a, p) => a + p[1], 0) / pts.length;
  return (seg.freq?.unit === 'hz' ? 1 : S.project.device.f0) * m;
}

// ---------------------------------------------------------------- library
function renderLibrary() {
  const lib = $('lib'); lib.replaceChildren();
  const p = S.project; const dev = p.device; const ram = p.ram.waves;
  const issuesFor = (path) => S.issues.filter((i) => i.path.startsWith(path) && i.level === 'error').length;
  const isSel = (c, id) => S.sel?.coll === c && S.sel?.id === id;
  const select = (c, id) => { S.sel = { coll: c, id }; S.seg = null; renderLibrary(); renderInspector(); requestRender(); };

  const lens = S.lens || {};
  let used = 1 + 4 * ram.length; for (const id of ram) used += lens[id] || 0;
  const cap = dev.ram_end - dev.base_addr + 1;
  lib.append(el('h3', {}, `RAM ${ram.length}/${dev.max_waves}`, el('span', { class: 'grow' }), el('span', { class: 'muted' }, `${used}/${cap} B`)));
  const bar = el('div', { class: `bar${used > cap ? ' over' : ''}` }, el('div', { style: `width:${Math.min(100, used / cap * 100)}%` }));
  lib.append(bar);
  const ul = el('ul', { class: 'list ram' });
  ram.forEach((id, i) => {
    const w = byId('waves', id);
    const li = el('li', { class: isSel('waves', id) ? 'sel' : '', draggable: 'true', onclick: () => select('waves', id) },
      el('span', { class: 'r' }, i + 1), el('span', { class: `id prov-${(w?.provenance || '').split(':')[0]}` }, id),
      el('span', { class: 'r' }, lens[id] ?? ''), el('span', { class: 'r' }, lens[id] ? fmt(lens[id] * 1000 / dev.fs, 1) : ''));
    li.addEventListener('dragstart', (e) => e.dataTransfer.setData('text/ram', String(i)));
    li.addEventListener('dragover', (e) => { e.preventDefault(); li.classList.add('dragover'); });
    li.addEventListener('dragleave', () => li.classList.remove('dragover'));
    li.addEventListener('drop', (e) => {
      e.preventDefault(); const from = +e.dataTransfer.getData('text/ram');
      edit((pp) => { const [x] = pp.ram.waves.splice(from, 1); pp.ram.waves.splice(i, 0, x); });
    });
    ul.append(li);
  });
  lib.append(ul);

  lib.append(el('h3', {}, 'waves', el('span', { class: 'grow' }), el('button', { onclick: newWave, title: 'new parametric wave' }, '+')));
  const wl = el('ul', { class: 'list waves' });
  for (const w of coll('waves')) {
    wl.append(el('li', { class: isSel('waves', w.id) ? 'sel' : '', onclick: () => select('waves', w.id), title: w.provenance },
      el('span', { class: 'muted' }, ram.includes(w.id) ? '▣' : '·'),
      el('span', { class: `id prov-${(w.provenance || '').split(':')[0]}` }, w.id, issuesFor(`waves[${coll('waves').indexOf(w)}]`) ? el('span', { class: 'bad' }, ' !') : ''),
      el('span', { class: 'r' }, lens[w.id] ? fmt(lens[w.id] * 1000 / dev.fs, 1) : '')));
  }
  lib.append(wl);

  lib.append(el('h3', {}, 'sequences', el('span', { class: 'grow' }), el('button', { onclick: newSequence, title: 'new sequence' }, '+')));
  const sl = el('ul', { class: 'list seqs' });
  for (const s of coll('sequences')) {
    sl.append(el('li', { class: isSel('sequences', s.id) ? 'sel' : '', onclick: () => select('sequences', s.id) },
      el('span', { class: 'id' }, s.id), el('span', { class: 'r' }, `${s.slots.length} sl`)));
  }
  lib.append(sl);

  lib.append(el('h3', {}, 'effects'));
  const el2 = el('ul', { class: 'list effs' });
  for (const e of coll('effects')) {
    el2.append(el('li', { class: `${isSel('effects', e.id) ? 'sel' : ''} ${e.enabled ? '' : 'disabled'}`, onclick: () => select('effects', e.id), title: `${e.kind} ${e.aidl}` },
      el('span', { class: 'muted' }, e.kind === 'primitive' ? 'p' : 'e'), el('span', { class: 'id' }, e.name),
      el('span', { class: 'r' }, e.sequence || e.stream_wave || '–'), el('span', { class: 'r' }, e.enabled ? 'on' : 'off')));
  }
  lib.append(el2);

  lib.append(el('h3', {}, 'references'));
  const rl = el('ul', { class: 'list refs' });
  for (const r of coll('references')) {
    rl.append(el('li', { class: isSel('references', r.id) ? 'sel' : '', onclick: () => select('references', r.id), title: r.source },
      el('span', { class: 'id' }, r.name || r.id), el('span', { class: 'r' }, r.fs), el('span', { class: 'r' }, r.n ? fmt(r.n * 1000 / r.fs, 1) : '')));
  }
  lib.append(rl);
}

// ---------------------------------------------------------------- inspector helpers
function num(value, onchange, attrs = {}) {
  return el('input', { type: 'number', class: `num ${attrs.cls || 'w5'}`, value, step: attrs.step ?? 'any', min: attrs.min, max: attrs.max, title: attrs.title,
    onchange: (e) => { const v = parseFloat(e.target.value); if (!Number.isNaN(v)) onchange(v); } });
}
function txt(value, onchange, attrs = {}) {
  return el('input', { type: 'text', value: value ?? '', spellcheck: 'false', class: attrs.cls, title: attrs.title, onchange: (e) => onchange(e.target.value) });
}
function sel(options, value, onchange, attrs = {}) {
  return el('select', { class: attrs.cls, title: attrs.title, onchange: (e) => onchange(e.target.value) },
    options.map((o) => { const [v, l] = Array.isArray(o) ? o : [o, o]; return el('option', { value: v, selected: v === value ? 'selected' : null }, l); }));
}
const ptsToText = (pts) => (pts || []).map(([u, v]) => `${+u.toFixed(4)}:${+Number(v).toFixed(4)}`).join(' ');
function textToPts(t) {
  const parts = t.trim().split(/[\s,;]+/).filter(Boolean);
  if (parts.length === 1 && !parts[0].includes(':')) { const v = parseFloat(parts[0]); return [[0, v], [1, v]]; }
  const pts = parts.map((p) => p.split(':').map(Number));
  if (pts.some((p) => p.length !== 2 || p.some(Number.isNaN))) throw new Error(`bad points "${t}"`);
  return pts.sort((a, b) => a[0] - b[0]);
}
const kv = (...pairs) => el('div', { class: 'kv' }, pairs.flatMap(([k, v]) => [el('span', {}, k), v]));

function newTone(amp = 1, cycles = 2, label = 'drive') {
  return { type: 'tone', length: { unit: 'cycles', value: cycles }, freq: { unit: 'f0', points: [[0, 1], [1, 1]], interp: 'linear' },
    amp: { points: [[0, amp], [1, amp]], interp: 'linear' }, shape: { kind: 'sine', sharp: 0 }, phase: { mode: 'continue', deg: 0 }, label };
}
function uniqueId(base) {
  const taken = new Set(['waves', 'sequences', 'effects', 'references'].flatMap((c) => coll(c).map((x) => x.id)));
  let id = base.toLowerCase().replace(/[^a-z0-9_]+/g, '_').slice(0, 36) || 'item'; let k = 2; const b = id;
  while (taken.has(id)) id = `${b}_${k++}`;
  return id;
}
function newWave() {
  const id = uniqueId('wave');
  edit((p) => p.waves.push({ id, name: id, provenance: 'original', segments: [newTone(1, 2), newTone(-0.5, 1, 'brake')], post: { gain: 1 }, notes: '' }));
  S.sel = { coll: 'waves', id }; renderLibrary(); renderInspector(); requestRender(true);
}
function newSequence() {
  const id = uniqueId('seq');
  const first = S.project.ram.waves[0];
  edit((p) => p.sequences.push({ id, name: '', slots: first ? [{ kind: 'wave', wave: first, loops: 0 }] : [], mainloop: 0, notes: '' }));
  S.sel = { coll: 'sequences', id }; renderLibrary(); renderInspector(); requestRender(true);
}
function renameRefs(p, coll_, oldId, newId) {
  if (coll_ === 'waves') {
    p.ram.waves = p.ram.waves.map((x) => (x === oldId ? newId : x));
    for (const s of p.sequences) for (const sl of s.slots) if (sl.wave === oldId) sl.wave = newId;
    for (const e of p.effects) if (e.stream_wave === oldId) e.stream_wave = newId;
  } else if (coll_ === 'sequences') for (const e of p.effects) if (e.sequence === oldId) e.sequence = newId;
}
function rename(c, item, v) {
  const nid = v.trim();
  if (!/^[a-z0-9_]{1,40}$/.test(nid)) { log(`invalid id "${nid}"`, 'error'); renderInspector(); return; }
  if (nid === item.id) return;
  if (coll(c).some((x) => x.id === nid)) { log(`id "${nid}" exists`, 'error'); renderInspector(); return; }
  const old = item.id;
  edit((p) => { p[c].find((x) => x.id === old).id = nid; renameRefs(p, c, old, nid); });
  S.sel = { coll: c, id: nid }; renderLibrary(); renderInspector();
}
function removeItem(c, id) {
  edit((p) => {
    p[c] = p[c].filter((x) => x.id !== id);
    if (c === 'waves') { p.ram.waves = p.ram.waves.filter((x) => x !== id); for (const e of p.effects) if (e.stream_wave === id) e.stream_wave = null; }
    if (c === 'sequences') for (const e of p.effects) if (e.sequence === id) { e.sequence = null; }
  });
  S.sel = defaultSel(); renderLibrary(); renderInspector(); requestRender();
}
function duplicate(c, item) {
  const id = uniqueId(`${item.id}_copy`); const copy = clone(item); copy.id = id;
  if (c === 'waves' && (copy.provenance === 'stock' || copy.provenance.startsWith('reference:'))) { /* provenance carries */ }
  edit((p) => p[c].push(copy));
  S.sel = { coll: c, id }; renderLibrary(); renderInspector(); requestRender();
}

// ---------------------------------------------------------------- inspector
function renderInspector() {
  const box = $('inspBody'); box.replaceChildren();
  if (!S.sel) return;
  const item = byId(S.sel.coll, S.sel.id); if (!item) return;
  ({ waves: inspectWave, sequences: inspectSequence, effects: inspectEffect, references: inspectReference })[S.sel.coll](box, item);
  renderInspectorIssues();
}

function inspectWave(box, w) {
  const inRam = S.project.ram.waves.includes(w.id);
  box.append(el('h3', {}, `wave`, el('span', { class: 'grow' }),
    el('button', { onclick: () => duplicate('waves', w), title: 'duplicate' }, '⧉'),
    el('button', { class: 'danger', onclick: () => removeItem('waves', w.id), title: 'delete' }, '✕')));
  box.append(kv(
    ['id', txt(w.id, (v) => rename('waves', w, v))],
    ['name', txt(w.name, (v) => edit(() => { w.name = v; }))],
    ['provenance', txt(w.provenance, (v) => edit(() => { w.provenance = v.trim(); }), { title: 'original | stock | reference:<id> | traced:<id>' })],
    ['gain', num(w.post?.gain ?? 1, (v) => edit(() => { w.post = { ...(w.post || {}), gain: v }; }), { step: 0.01, cls: 'w4' })],
    ['RAM', el('label', { class: 'chk' }, el('input', { type: 'checkbox', checked: inRam, onchange: (e) => edit((p) => {
      p.ram.waves = e.target.checked ? [...p.ram.waves, w.id] : p.ram.waves.filter((x) => x !== w.id);
    }) }), inRam ? `#${S.project.ram.waves.indexOf(w.id) + 1}` : 'no')],
    ['notes', el('textarea', { rows: 2, onchange: (e) => edit(() => { w.notes = e.target.value; }) }, w.notes || '')],
  ));
  box.append(el('h3', {}, 'segments', el('span', { class: 'grow' }),
    el('button', { onclick: () => edit(() => w.segments.push(newTone(1, 1))) }, '+tone'),
    el('button', { onclick: () => edit(() => w.segments.push({ type: 'gap', length: { unit: 'ms', value: 5 }, label: 'gap' })) }, '+gap'),
    el('button', { onclick: () => edit(() => w.segments.push(newTone(-0.5, 1, 'brake'))) }, '+brake'),
    el('button', { class: 'primary', onclick: () => optimizeBrake(w), title: 'optimise selected (or append) brake segment against the LRA model' }, 'brake ⚙')));
  w.segments.forEach((s, i) => box.append(segmentCard(w, s, i)));
}

function segmentCard(w, s, i) {
  const brake = s.type === 'tone' && (s.amp?.points || []).every((p) => p[1] <= 0);
  const move = (d) => edit(() => { const j = i + d; if (j < 0 || j >= w.segments.length) return; [w.segments[i], w.segments[j]] = [w.segments[j], w.segments[i]]; });
  const card = el('div', { class: `seg ${brake ? 'brake' : ''} ${S.seg === i ? 'selseg' : ''}`, onclick: () => { S.seg = i; } });
  card.append(el('div', { class: 'hd' },
    el('span', { class: 't' }, `${i}`),
    sel(['tone', 'gap', 'pcm'].filter((t) => t !== 'pcm' || s.type === 'pcm'), s.type, (v) => edit(() => {
      w.segments[i] = v === 'gap' ? { type: 'gap', length: { unit: 'ms', value: 5 }, label: s.label } : newTone(1, 1, s.label);
    })),
    txt(s.label, (v) => edit(() => { s.label = v; }), { cls: 'w6', title: 'label' }),
    el('span', { class: 'grow' }),
    el('button', { onclick: () => move(-1), title: 'up' }, '↑'), el('button', { onclick: () => move(1), title: 'down' }, '↓'),
    el('button', { onclick: () => edit(() => w.segments.splice(i + 1, 0, clone(s))), title: 'duplicate' }, '⧉'),
    el('button', { class: 'danger', onclick: () => edit(() => w.segments.splice(i, 1)), title: 'delete' }, '✕')));
  const bd = el('div', { class: 'bd' });
  const len = () => el('div', { class: 'row' }, num(s.length.value, (v) => edit(() => { s.length.value = v; }), { step: s.length.unit === 'cycles' ? 0.5 : 0.1 }),
    sel(['cycles', 'ms', 'samples'], s.length.unit, (v) => edit(() => { s.length.unit = v; })));
  if (s.type === 'tone') {
    s.freq ||= { unit: 'f0', points: [[0, 1], [1, 1]], interp: 'linear' };
    s.amp ||= { points: [[0, 1], [1, 1]], interp: 'linear' };
    s.shape ||= { kind: 'sine', sharp: 0 }; s.phase ||= { mode: 'continue', deg: 0 };
    const ptsInput = (spec) => txt(ptsToText(spec.points), (v) => { try { const pts = textToPts(v); edit(() => { spec.points = pts; }); } catch (e) { log(e.message, 'error'); renderInspector(); } }, { cls: 'pts', title: 'u:value pairs, u in 0..1' });
    bd.append(el('span', {}, 'len'), len(),
      el('span', {}, 'freq'), el('div', { class: 'row' }, sel([['f0', '×f0'], ['hz', 'Hz']], s.freq.unit, (v) => edit(() => {
        const k = v === 'hz' ? S.project.device.f0 : 1 / S.project.device.f0; s.freq.points = s.freq.points.map(([u, x]) => [u, +(x * k).toFixed(4)]); s.freq.unit = v;
      })), sel(['linear', 'cos', 'exp'], s.freq.interp || 'linear', (v) => edit(() => { s.freq.interp = v; }))),
      el('span', {}, ''), ptsInput(s.freq),
      el('span', {}, 'amp'), el('div', { class: 'row' }, ptsInput(s.amp)),
      el('span', {}, ''), el('div', { class: 'row' }, sel(['linear', 'cos', 'exp'], s.amp.interp || 'linear', (v) => edit(() => { s.amp.interp = v; })),
        el('button', { title: 'invert (brake)', onclick: () => edit(() => { s.amp.points = s.amp.points.map(([u, v]) => [u, -v]); }) }, '±')),
      el('span', {}, 'shape'), el('div', { class: 'row' }, sel(['sine', 'triangle'], s.shape.kind, (v) => edit(() => { s.shape.kind = v; })),
        el('label', {}, 'sharp', num(s.shape.sharp ?? 0, (v) => edit(() => { s.shape.sharp = Math.min(1, Math.max(0, v)); }), { step: 0.05, cls: 'w4' }))),
      el('span', {}, 'phase'), el('div', { class: 'row' }, sel(['continue', 'absolute'], s.phase.mode, (v) => edit(() => { s.phase.mode = v; })),
        num(s.phase.deg ?? 0, (v) => edit(() => { s.phase.deg = v; }), { step: 5, cls: 'w4' }), '°'));
  } else if (s.type === 'gap') {
    bd.append(el('span', {}, 'len'), len());
  } else if (s.type === 'pcm') {
    const n = Math.floor(atob(s.data_b64 || '').length / (s.encoding === 'f32' ? 4 : 1));
    bd.append(el('span', {}, 'data'), el('span', { class: 'muted' }, `${n} × ${s.encoding} @ ${s.fs} Hz`),
      el('span', {}, 'gain'), num(s.gain ?? 1, (v) => edit(() => { s.gain = v; }), { step: 0.01, cls: 'w4' }),
      el('span', {}, 'trim'), el('div', { class: 'row' },
        num(s.trim?.[0] ?? 0, (v) => edit(() => { s.trim = [Math.max(0, Math.round(v)), s.trim?.[1] ?? n]; }), { step: 1, cls: 'w5' }),
        num(s.trim?.[1] ?? n, (v) => edit(() => { s.trim = [s.trim?.[0] ?? 0, Math.min(n, Math.round(v))]; }), { step: 1, cls: 'w5' })));
  }
  card.append(bd);
  return card;
}

function inspectSequence(box, s) {
  const ram = S.project.ram.waves; const dev = S.project.device;
  box.append(el('h3', {}, 'sequence', el('span', { class: 'grow' }),
    el('button', { onclick: () => duplicate('sequences', s), title: 'duplicate' }, '⧉'),
    el('button', { class: 'danger', onclick: () => removeItem('sequences', s.id), title: 'delete' }, '✕')));
  box.append(kv(['id', txt(s.id, (v) => rename('sequences', s, v))],
    ['mainloop', num(s.mainloop ?? 0, (v) => edit(() => { s.mainloop = Math.max(0, Math.min(14, Math.round(v))); }), { step: 1, cls: 'w4' })],
    ['notes', el('textarea', { rows: 2, onchange: (e) => edit(() => { s.notes = e.target.value; }) }, s.notes || '')]));
  box.append(el('h3', {}, 'slots', el('span', { class: 'grow' }), el('span', { class: 'muted' }, `unit ${dev.wait_unit} smp · plays = loops + ${dev.loop_extra_plays}`)));
  for (let i = 0; i < 8; i++) {
    const sl = s.slots[i];
    const kind = sl ? sl.kind : '';
    const row = el('div', { class: 'slot' }, el('span', { class: 'muted' }, i + 1),
      sel([['', '—'], ['wave', 'wave'], ['wait', 'wait']], kind, (v) => edit(() => {
        if (!v) s.slots.splice(i); else s.slots[i] = v === 'wave' ? { kind: 'wave', wave: ram[0] || '', loops: 0 } : { kind: 'wait', ms: 10 };
        s.slots = s.slots.filter(Boolean);
      }), { disabled: !sl && i > s.slots.length }));
    if (sl?.kind === 'wave') {
      row.append(sel(ram.map((id, k) => [id, `${k + 1} ${id}`]).concat(ram.includes(sl.wave) ? [] : [[sl.wave, `${sl.wave} (not in RAM)`]]), sl.wave, (v) => edit(() => { sl.wave = v; })),
        el('label', {}, '↻', num(sl.loops ?? 0, (v) => edit(() => { sl.loops = Math.max(0, Math.min(14, Math.round(v))); }), { step: 1, cls: 'w3', title: 'loop register 0..14' })));
    } else if (sl?.kind === 'wait') {
      const n = Math.max(1, Math.min(127, Math.round(sl.ms * dev.fs / 1000 / dev.wait_unit)));
      row.append(num(sl.ms, (v) => edit(() => { sl.ms = Math.max(0.05, v); }), { step: 1, cls: 'w5' }),
        el('span', { class: 'eff' }, `${fmt(n * dev.wait_unit * 1000 / dev.fs)} ms`));
    } else row.append(el('span'), el('span'));
    box.append(row);
  }
  const r = S.render;
  if (r?.registers) box.append(el('div', { class: 'muted small' }, `WAVCFG1-8 ${r.registers.wavcfg.map((v) => v.toString(16).padStart(2, '0')).join(' ')} · loops ${r.registers.loops.join(' ')} · main ${r.registers.mainloop}`));
}

function inspectEffect(box, e) {
  box.append(el('h3', {}, `${e.kind} ${e.name} (${e.aidl})`));
  box.append(kv(
    ['enabled', el('input', { type: 'checkbox', checked: e.enabled, onchange: (ev) => edit(() => { e.enabled = ev.target.checked; }) })],
    ['sequence', sel([['', '—'], ...coll('sequences').map((s) => s.id)], e.sequence || '', (v) => edit(() => { e.sequence = v || null; }))],
    ['stream wave', sel([['', '— (render sequence)'], ...waveIds()], e.stream_wave || '', (v) => edit(() => { e.stream_wave = v || null; }))],
    ['scale', num(e.scale ?? 1, (v) => edit(() => { e.scale = v; }), { step: 0.05, cls: 'w4' })],
    ['notes', el('textarea', { rows: 3, onchange: (ev) => edit(() => { e.notes = ev.target.value; }) }, e.notes || '')]));
  const seq = e.sequence && byId('sequences', e.sequence);
  if (seq) box.append(el('div', { class: 'row' }, el('button', { onclick: () => { S.sel = { coll: 'sequences', id: seq.id }; renderLibrary(); renderInspector(); requestRender(); } }, `→ ${seq.id}`)));
}

function inspectReference(box, r) {
  box.append(el('h3', {}, 'reference', el('span', { class: 'grow' }), el('button', { class: 'danger', onclick: () => removeItem('references', r.id) }, '✕')));
  box.append(kv(['id', el('span', {}, r.id)], ['name', txt(r.name, (v) => edit(() => { r.name = v; }))], ['source', el('span', { class: 'small' }, r.source)],
    ['fs', el('span', {}, r.fs)], ['samples', el('span', {}, r.n ?? '')], ['proprietary', el('span', { class: r.proprietary ? 'badv' : '' }, r.proprietary ? 'yes' : 'no')],
    ['meta', el('pre', { class: 'small' }, JSON.stringify(r.meta || {}, null, 1))]));
  box.append(el('div', { class: 'row' },
    el('button', { onclick: () => { $('ov_ref').value = r.id; requestRender(); } }, 'overlay'),
    el('button', { onclick: () => refToWave(r) }, '→ pcm wave'),
    el('button', { onclick: () => fitQ(r), title: 'fit f0 / Q from a burst + ringdown recording' }, 'fit f0/Q')));
}

async function fitQ(r) {
  try {
    await api('PUT', '/api/project', { project: S.project });
    const f = await api('POST', '/api/fit', { ref: r.id });
    const dev = S.project.device;
    const row = (k, v, cur, key) => el('tr', {}, el('td', {}, k), el('td', {}, v ?? '–'), el('td', {}, cur ?? ''),
      el('td', {}, key && v != null ? el('input', { type: 'checkbox', 'data-key': key, 'data-val': v, checked: key !== 'f0' }) : ''));
    const body = el('div', {}, el('h3', {}, `fit ${r.id}`),
      el('table', { class: 'mt' }, el('tr', {}, el('th', {}, ''), el('th', {}, 'fit'), el('th', {}, 'device'), el('th', {}, 'apply')),
        row('f0 Hz', f.f0, dev.f0, 'f0'), row('Q free', f.q_free, dev.q_free, 'q_free'), row('Q drive', f.q_drive, dev.q_drive, 'q_drive')),
      el('div', { class: 'muted small' }, `decay ${f.decay ? `${f.decay.t0_ms}–${f.decay.t1_ms} ms r² ${f.decay.r2}` : '–'} · ` +
        `rise ${f.rise ? `${f.rise.t0_ms}–${f.rise.t1_ms} ms r² ${f.rise.r2} reached ${f.rise.reached}` : '–'} · SNR ${f.snr_db ?? '–'} dB`));
    log(`fit ${r.id}: f0 ${f.f0} Q_free ${f.q_free} Q_drive ${f.q_drive}`);
    if (!(await modal(body, 'apply'))) return;
    const sel_ = [...body.querySelectorAll('input[type=checkbox]:checked')];
    if (sel_.length) edit((p) => { for (const c of sel_) p.device[c.dataset.key] = +c.dataset.val; });
  } catch (e) { log(`fit: ${e.message}`, 'error'); }
}

async function refToWave(r) {
  const mode = $('ov_mode').value, fref = +$('ov_fref').value || 0;
  try {
    const res = await api('POST', '/api/reference/to_wave', { project: S.project, id: r.id, retarget: mode, f_ref: fref });
    S.undo.push(clone(S.project)); S.redo = [];
    setProject(res.project, { dirty: true }); S.sel = { coll: 'waves', id: res.wave }; renderLibrary(); renderInspector(); requestRender();
    log(`reference ${r.id} → wave ${res.wave} (${mode})`);
  } catch (e) { log(e.message, 'error'); }
}

const BRAKE_PREF = 'hapkit.brakeOpt';
function brakePrefs() {
  let v = null;
  try { v = JSON.parse(localStorage.getItem(BRAKE_PREF) || 'null'); } catch { /* storage unavailable */ }
  return { ramp_in: 0, ramp_out: 0, taper: 0, robust: true, ...(v || {}) };
}
function saveBrakePrefs(st) { try { localStorage.setItem(BRAKE_PREF, JSON.stringify(st)); } catch { /* ignore */ } }

async function optimizeBrake(w) {
  // selected brake segment, else the last segment if it is a brake, else append one
  const isBrake = (i) => w.segments[i]?.type === 'tone' && (w.segments[i].amp?.points || []).every((p) => p[1] <= 0);
  const last = w.segments.length - 1;
  const idx = S.seg != null && isBrake(S.seg) ? S.seg : (last >= 1 && isBrake(last) ? last : null);
  const st = brakePrefs();
  let r = null, chosen = null;
  const num = (k, title) => el('input', { type: 'number', step: 0.25, min: 0, value: st[k], title, style: 'width:56px',
    onchange: (e) => { st[k] = Math.max(0, +e.target.value || 0); saveBrakePrefs(st); } });
  const out = el('div');
  const run = async (sweep) => {
    out.replaceChildren(el('div', { class: 'muted' }, sweep ? 'sweeping ramp-in…' : 'optimising…'));
    log(`brake optimise ${w.id} ${idx == null ? '(append)' : `segment ${idx}`} in ${st.ramp_in} out ${st.ramp_out} taper ${st.taper}${st.robust ? ' robust' : ''}${sweep ? ' sweep' : ''}`);
    try {
      await api('PUT', '/api/project', { project: S.project });
      r = await api('POST', '/api/brake/optimize', { wave: w.id, segment: idx, robust: st.robust,
        soft: { ramp_in: st.ramp_in, ramp_out: st.ramp_out, taper: st.taper }, sweep });
    } catch (e) { out.replaceChildren(el('div', { class: 'badv' }, e.message)); return; }
    const cands = r.candidates || [r];
    chosen = cands[0];
    const head = el('tr', {}, ...['in', 'out', 'taper', 'cyc', 'amp', 'φ°', 'ms', 'click dB', 'resid dB', 'worst dB', 'ring ms'].map((h) => el('th', {}, h)));
    const b = r.before;
    const rdb = (v) => (v == null ? null : 10 * Math.log10(Math.max(v, 1e-9)));
    const rows = [el('tr', { class: 'muted' }, el('td', {}, 'unbraked'), el('td', {}), el('td', {}), el('td', {}), el('td', {}), el('td', {}),
      el('td', {}, fmt(b.active_ms, 1)), el('td', {}, fmt(b.click_db, 1)), el('td', {}, fmt(rdb(b.residual_ratio), 1)), el('td', {}), el('td', {}, fmt(b.ring_ms, 1)))];
    for (const c of cands) {
      const a = c.after, p = c.params, sf = c.soft;
      const tr = el('tr', { class: c === chosen ? 'sel' : '', style: 'cursor:pointer' },
        el('td', {}, fmt(sf.ramp_in, 2)), el('td', {}, fmt(sf.ramp_out, 2)), el('td', {}, fmt(sf.taper, 2)), el('td', {}, fmt(p.cycles, 2)),
        el('td', {}, fmt(p.amp, 3)), el('td', {}, fmt(p.phase_deg, 1)), el('td', {}, fmt(a.length_ms, 1)), el('td', {}, fmt(a.click_db, 1)),
        el('td', {}, fmt(rdb(a.residual_ratio), 1)), el('td', {}, fmt(rdb(a.residual_worst), 1)), el('td', {}, fmt(a.ring_ms, 1)));
      tr.onclick = () => { chosen = c; for (const x of tr.parentNode.querySelectorAll('tr')) x.classList.remove('sel'); tr.classList.add('sel'); };
      rows.push(tr);
    }
    out.replaceChildren(el('table', { class: 'mt brk' }, head, ...rows),
      el('div', { class: 'muted small' }, 'in/out/taper in cycles · click: energy above 2.5 f0 around the brake, rel. total · resid: energy after the end / during, model (worst: over f0 ±0.3 %, Qd ±15 %)'));
  };
  const robust = el('input', { type: 'checkbox', checked: st.robust, onchange: (e) => { st.robust = e.target.checked; saveBrakePrefs(st); } });
  const body = el('div', {}, el('h3', {}, `brake ${w.id} ${idx == null ? '(append)' : `seg ${idx}`}`),
    el('div', { class: 'row' }, 'ramp in', num('ramp_in', 'brake amplitude rises from 0 over this many cycles'),
      'out', num('ramp_out', 'brake amplitude falls to 0 over this many cycles'),
      'taper', num('taper', 'fade the end of the preceding tone segment to 0 over this many cycles'),
      el('label', {}, robust, 'robust'),
      el('button', { type: 'button', onclick: () => run(null) }, 'optimise'),
      el('button', { type: 'button', title: 'optimise for ramp-in 0, 0.5, 1, 1.5, 2 cycles', onclick: () => run([0, 0.5, 1, 1.5, 2]) }, 'sweep in')),
    out);
  run(null);
  if (await modal(body, 'apply') && chosen) {
    const c = chosen;
    edit(() => {
      if (r.append) w.segments.push(c.segment); else w.segments[r.index] = c.segment;
      if (c.prev_segment && c.prev_index != null) w.segments[c.prev_index] = c.prev_segment;
    });
    S.seg = r.index;
  }
}

// ---------------------------------------------------------------- modal
function modal(content, okLabel = 'ok') {
  return new Promise((resolve) => {
    $('modalBody').replaceChildren(content); $('modalOk').textContent = okLabel; $('modal').hidden = false;
    const done = (v) => { $('modal').hidden = true; $('modalOk').onclick = $('modalCancel').onclick = null; resolve(v); };
    $('modalOk').onclick = () => done(true); $('modalCancel').onclick = () => done(false);
  });
}

// ---------------------------------------------------------------- top bar
function renderTop() {
  const p = S.project;
  $('projName').value = p.name || '';
  $('projPath').textContent = S.path ? S.path.split('/').slice(-1)[0] : '(unsaved)';
  $('dirty').hidden = !S.dirty;
  for (const k of ['fs', 'f0', 'q_drive', 'q_free']) if (document.activeElement !== $(`dev_${k}`)) $(`dev_${k}`).value = p.device[k];
  const cur = $('ov_ref').value;
  const opts = [['', '—']].concat(coll('references').map((r) => [r.id, `ref ${r.name || r.id}`]),
    waveIds().map((id) => [`wave:${id}`, `wave ${id}`]), coll('sequences').map((s) => [`sequence:${s.id}`, `seq ${s.id}`]));
  $('ov_ref').replaceChildren(...opts.map(([v, l]) => el('option', { value: v, selected: v === cur ? 'selected' : null }, l)));
  $('undo').disabled = !S.undo.length; $('redo').disabled = !S.redo.length;
}

async function syncState() {
  const st = await api('GET', '/api/state');
  S.issues = st.issues || [];
  setProject(st.project, { path: st.path, dirty: st.dirty });
}

function refreshAll() { renderTop(); renderLibrary(); renderInspector(); }
function renderInspectorIssues() {
  const box = $('inspBody'); box.querySelectorAll('.issue').forEach((n) => n.remove());
  const item = byId(S.sel.coll, S.sel.id); if (!item) return;
  const pre = `${S.sel.coll}[${coll(S.sel.coll).indexOf(item)}]`;
  for (const i of S.issues.filter((x) => x.path === pre || x.path.startsWith(`${pre}.`))) {
    box.append(el('div', { class: `issue ${i.level === 'error' ? 'badv' : 'muted'}` }, `${i.level}: ${i.path.slice(pre.length + 1) || ''} ${i.msg}`));
  }
}

async function projectMenu(v) {
  try {
    if (v.startsWith('new:')) {
      if (S.dirty && !(await modal(el('div', {}, 'discard unsaved changes?'), 'discard'))) return;
      const st = await api('POST', '/api/project/new', { template: v.slice(4), name: v.slice(4) });
      S.undo = []; S.redo = []; S.issues = st.issues || []; S.sel = null; setProject(st.project, { path: null, dirty: true }); requestRender();
    } else if (v === 'open') {
      const { files } = await api('GET', '/api/projects');
      const pick = sel(files.map((f) => [f.path, `${f.name}  ${new Date(f.mtime * 1000).toLocaleString()}`]), files[0]?.path, () => {});
      pick.size = Math.min(12, Math.max(3, files.length)); pick.style.height = 'auto'; pick.style.width = '100%';
      if (!files.length || !(await modal(el('div', {}, el('h3', {}, 'open'), pick), 'open'))) return;
      const st = await api('POST', '/api/project/open', { path: pick.value });
      S.undo = []; S.redo = []; S.issues = st.issues || []; S.sel = null; setProject(st.project, { path: st.path, dirty: false }); requestRender();
    } else if (v === 'save' || v === 'saveas') {
      let path = v === 'save' ? null : undefined;
      if (v === 'saveas' || !S.path) {
        const inp = txt(S.project.name || 'project', () => {}); inp.style.width = '100%';
        if (!(await modal(el('div', {}, el('h3', {}, 'save as (projects/…)'), inp), 'save'))) return;
        path = inp.value.trim();
      }
      await api('PUT', '/api/project', { project: S.project });
      const r = await api('POST', '/api/project/save', path ? { path } : {});
      S.path = r.path; S.dirty = false; renderTop(); log(`saved ${r.path}`);
    }
  } catch (e) { log(e.message, 'error'); }
}

async function importMenu(kind) {
  const fi = $('fileInput'); fi.value = '';
  fi.accept = { ram_bin: '.bin', wav: '.wav', raw: '', carray: '.c,.cpp,.h,.cc', cirrus: '.bin', csv: '.csv,.txt,.tsv' }[kind] ?? '';
  fi.multiple = kind === 'wav';
  fi.onchange = async () => {
    const files = [...fi.files]; if (!files.length) return;
    const opts = { as: 'reference' };
    const asSel = sel([['reference', 'reference'], ['wave', 'pcm wave']], kind === 'ram_bin' ? 'wave' : 'reference', (v) => { opts.as = v; });
    const fsIn = num(kind === 'raw' ? S.project.device.fs : 24000, () => {}, { cls: 'w6' });
    const encSel = sel(['int8', 'int16le', 'f32'], 'int8', () => {});
    const ramSel = sel([['none', 'keep RAM list'], ['append', 'append to RAM'], ['replace', 'replace RAM list']], 'none', () => {});
    const prop = el('input', { type: 'checkbox', checked: kind === 'cirrus' });
    const prefix = txt(files[0].name.replace(/\..*$/, '').toLowerCase().replace(/[^a-z0-9_]+/g, '_').slice(0, 16), () => {});
    const rows = [['files', el('span', {}, files.map((f) => f.name).join(', '))]];
    if (kind !== 'ram_bin') rows.push(['import as', asSel]);
    if (kind === 'raw') rows.push(['fs', fsIn], ['encoding', encSel]);
    if (kind === 'carray') rows.push(['default fs', fsIn]);
    const csvFs = el('input', { type: 'number', class: 'num w6', placeholder: 'from time col' });
    const csvAxis = txt('auto', () => {}, { cls: 'w6', title: 'auto (largest variance) | column name or index | mag' });
    if (kind === 'csv') rows.push(['fs', csvFs], ['axis', csvAxis]);
    if (kind === 'ram_bin') rows.push(['id prefix', prefix], ['RAM', ramSel]);
    rows.push(['proprietary', prop]);
    if (!(await modal(el('div', {}, el('h3', {}, `import ${kind}`), kv(...rows)), 'import'))) return;
    await api('PUT', '/api/project', { project: S.project });
    for (const f of files) {
      const buf = new Uint8Array(await f.arrayBuffer()); let bin = '';
      for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
      const o = { ...opts, proprietary: prop.checked };
      if (kind === 'raw') Object.assign(o, { fs: +fsIn.value, encoding: encSel.value });
      if (kind === 'carray') o.fs = +fsIn.value;
      if (kind === 'csv') Object.assign(o, { axis: csvAxis.value.trim() || 'auto', ...(+csvFs.value ? { fs: +csvFs.value } : {}) });
      if (kind === 'ram_bin') Object.assign(o, { as: 'wave', prefix: prefix.value, ram: ramSel.value, provenance: prop.checked ? 'stock' : 'original' });
      try {
        S.undo.push(clone(S.project)); S.redo = [];
        const r = await api('POST', '/api/import', { kind, filename: f.name, data_b64: btoa(bin), options: o });
        S.issues = r.issues || []; setProject(r.project, { dirty: true });
        log(`import ${f.name}: ${r.report} (+${r.added.waves.length} waves, +${r.added.references.length} refs)`);
      } catch (e) { S.undo.pop(); log(`import ${f.name}: ${e.message}`, 'error'); }
    }
  };
  fi.click();
}

async function exportMenu(kind) {
  try {
    await api('PUT', '/api/project', { project: S.project });
    const t = target();
    const r = await api('POST', '/api/export', { kind, target: t ? { type: t.type, id: t.id, route: t.route } : null, options: { upstream: $('upstream').checked } });
    const bin = atob(r.data_b64); const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    const a = el('a', { href: URL.createObjectURL(new Blob([buf], { type: r.mime })), download: r.filename });
    document.body.append(a); a.click(); a.remove();
    log(`export ${kind} → ${r.path}${r.report ? `\n${r.report}` : ''}`);
  } catch (e) { log(`export: ${e.message}`, 'error'); }
}

// ---------------------------------------------------------------- device
function devStatus(st) {
  S.dev = st || { connected: false };
  const pill = $('devPill');
  if (st?.connected) {
    pill.className = 'pill on';
    pill.textContent = `${st.serial} ${st.root ? 'root' : 'user'} ${st.selinux || ''} ${st.ram_tag ? `ram:${st.ram_tag}` : ''}`.trim();
  } else { pill.className = st?.error ? 'pill bad' : 'pill off'; pill.textContent = st?.error ? 'device: error' : 'no device'; }
  $('d_status').textContent = st?.error || (st?.connected ? `${st.lineage || ''} ${st.kernel || ''}` : '');
}
async function devCall(path, body, label) {
  try {
    if (body?.needProject) { await api('PUT', '/api/project', { project: S.project }); delete body.needProject; }
    const r = await api(path === '/api/device' ? 'GET' : 'POST', path, path === '/api/device' ? undefined : (body || {}));
    if (r.status) devStatus(r.status); else if ('connected' in r) devStatus(r);
    return r;
  } catch (e) { log(`${label}: ${e.message}`, 'error'); return null; }
}
const play = () => { const t = target(); if (t) devCall('/api/device/play', { needProject: true, target: t, route: S.route, gain: S.gain }, 'play'); };
const stop = () => devCall('/api/device/stop', {}, 'stop');

// ---------------------------------------------------------------- wiring
function wire() {
  $('projName').onchange = (e) => edit((p) => { p.name = e.target.value; });
  $('projMenu').onchange = (e) => { const v = e.target.value; e.target.value = ''; projectMenu(v); };
  $('importMenu').onchange = (e) => { const v = e.target.value; e.target.value = ''; if (v) importMenu(v); };
  $('exportMenu').onchange = (e) => { const v = e.target.value; e.target.value = ''; if (v) exportMenu(v); };
  $('undo').onclick = undo; $('redo').onclick = redo;
  for (const k of ['fs', 'f0', 'q_drive', 'q_free']) {
    $(`dev_${k}`).onchange = (e) => { const v = parseFloat(e.target.value); if (v > 0) edit((p) => { p.device[k] = k === 'fs' ? Math.round(v) : v; }); };
  }
  for (const id of ['v_sim', 'v_env', 'v_rob', 'v_tail', 'v_win', 'v_fmax', 'v_db', 'ov_ref', 'ov_off', 'ov_gain', 'ov_mode', 'ov_fref']) {
    $(id).addEventListener('change', () => requestRender());
  }
  $('d_connect').onclick = () => devCall('/api/device/connect', {}, 'connect');
  $('d_upload').onclick = () => devCall('/api/device/ram/upload', { needProject: true, source: 'project' }, 'upload');
  $('d_stock').onclick = () => devCall('/api/device/ram/upload', { source: 'stock' }, 'stock RAM');
  $('d_restore').onclick = () => devCall('/api/device/restore', {}, 'restore');
  $('d_route').onchange = (e) => { S.route = e.target.value; requestRender(); };
  $('d_gain').oninput = (e) => { S.gain = +e.target.value; $('d_gain_v').textContent = S.gain; };
  $('d_play').onclick = play; $('d_stop').onclick = stop;
  window.addEventListener('keydown', (e) => {
    const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); projectMenu('save'); return; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && !typing) { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
    if (typing || !$('modal').hidden) return;
    if (e.key === ' ') { e.preventDefault(); play(); }
    else if (e.key === 'Escape') stop();
    else if (e.key === 'f' || e.key === 'F') view.fit();
    else if (e.key === 'Delete' && S.sel?.coll === 'waves' && S.seg != null) {
      const w = byId('waves', S.sel.id); if (w?.segments[S.seg]) { const i = S.seg; S.seg = null; edit(() => w.segments.splice(i, 1)); }
    }
  });
  // middle button (wheel click) = Space; on mousedown for latency, default suppressed (autoscroll, X11 paste)
  window.addEventListener('mousedown', (e) => {
    if (e.button !== 1 || !$('modal').hidden || e.target.closest('input, textarea, select')) return;
    e.preventDefault(); play();
  });
  window.addEventListener('auxclick', (e) => { if (e.button === 1 && !e.target.closest('input, textarea, select')) e.preventDefault(); });
  window.addEventListener('beforeunload', (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ''; } });
}

let lensTimer = 0;
function scheduleLens() { clearTimeout(lensTimer); lensTimer = setTimeout(updateLens, 150); }
async function updateLens() {
  try {
    const r = await api('POST', '/api/lengths', { project: S.project });
    S.lens = r.lengths; S.issues = r.issues || S.issues; renderLibrary();
    if (S.sel) renderInspectorIssues();
  } catch (e) { log(`lengths: ${e.message}`, 'error'); }
}

async function main() {
  wire();
  await syncState();
  await updateLens();
  requestRender(true);
  devCall('/api/device', null, 'device');
  pollLog();
  window.__hapkit = { ready: true, S, view, api, edit, play, stop, requestRender,
    select: (c, id) => { S.sel = { coll: c, id }; S.seg = null; renderLibrary(); renderInspector(); requestRender(); } };
}

main().catch((e) => log(`init: ${e.message}`, 'error'));
