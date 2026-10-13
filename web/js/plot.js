// Canvas plots: shared time view, waveform, spectrogram, spectrum.

const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

export class View {
  constructor() { this.t0 = 0; this.t1 = 50; this.tmax = 50; this.listeners = []; }
  on(fn) { this.listeners.push(fn); }
  emit() { for (const fn of this.listeners) fn(); }
  set(t0, t1) {
    const span = Math.max(0.05, t1 - t0);
    this.t0 = t0; this.t1 = t0 + span; this.emit();
  }
  fit() { const pad = this.tmax * 0.02 + 0.2; this.set(-pad, this.tmax + pad); }
  zoom(f, tc) { this.set(tc - (tc - this.t0) * f, tc + (this.t1 - tc) * f); }
  pan(dt) { this.set(this.t0 + dt, this.t1 + dt); }
}

function niceStep(span, target) {
  const raw = span / Math.max(1, target);
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 5, 10]) if (m * p >= raw) return m * p;
  return 10 * p;
}

function fmtT(t, step) { return step < 0.1 ? t.toFixed(3) : step < 1 ? t.toFixed(2) : step < 10 ? t.toFixed(1) : t.toFixed(0); }

class Base {
  constructor(canvas) {
    this.c = canvas; this.ctx = canvas.getContext('2d'); this.dirty = true;
    new ResizeObserver(() => this.request()).observe(canvas);
  }
  request() { if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = 0; this.draw(); }); }
  size() {
    const dpr = window.devicePixelRatio || 1;
    const w = this.c.clientWidth, h = this.c.clientHeight;
    if (this.c.width !== Math.round(w * dpr) || this.c.height !== Math.round(h * dpr)) {
      this.c.width = Math.round(w * dpr); this.c.height = Math.round(h * dpr);
    }
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return [w, h];
  }
}

const M = { l: 46, r: 10, t: 16, b: 18 };

export class TimePlot extends Base {
  constructor(canvas, view, hooks = {}) {
    super(canvas);
    this.view = view; this.hooks = hooks; this.data = null; this.yzoom = 1;
    this.show = { env: true, sim: true };
    this.handles = []; this.drag = null; this.hover = null;
    view.on(() => this.request());
    canvas.addEventListener('wheel', (e) => this.onWheel(e), { passive: false });
    canvas.addEventListener('mousedown', (e) => this.onDown(e));
    window.addEventListener('mousemove', (e) => this.onMove(e));
    window.addEventListener('mouseup', (e) => this.onUp(e));
    canvas.addEventListener('mouseleave', () => { this.hover = null; this.hooks.hover?.(null); this.request(); });
    canvas.addEventListener('dblclick', () => this.view.fit());
  }
  setData(d) { this.data = d; this.request(); }
  setHandles(h) { this.handles = h || []; this.request(); }
  geom() {
    const w = this.c.clientWidth, h = this.c.clientHeight;
    const pw = Math.max(10, w - M.l - M.r), ph = Math.max(10, h - M.t - M.b);
    const v = this.view, ys = this.yzoom;
    return {
      w, h, pw, ph,
      x: (t) => M.l + (t - v.t0) / (v.t1 - v.t0) * pw,
      t: (x) => v.t0 + (x - M.l) / pw * (v.t1 - v.t0),
      y: (a) => M.t + (1 - (a / ys + 1) / 2) * ph,
      a: (y) => ((1 - (y - M.t) / ph) * 2 - 1) * ys,
    };
  }
  draw() {
    const [w, h] = this.size(); const ctx = this.ctx; const g = this.geom(); const d = this.data;
    ctx.fillStyle = css('--bg'); ctx.fillRect(0, 0, w, h);
    ctx.save(); ctx.beginPath(); ctx.rect(M.l, M.t, g.pw, g.ph); ctx.clip();
    if (d) {
      const fs = d.fs, toT = (i) => i * 1000 / fs;
      for (const [k, sb] of (d.slot_bounds || []).entries()) {
        ctx.fillStyle = k % 2 ? 'rgba(76,195,255,0.05)' : 'rgba(76,195,255,0.025)';
        if (sb.kind === 'wait') ctx.fillStyle = 'rgba(138,147,163,0.06)';
        ctx.fillRect(g.x(toT(sb.start)), M.t, g.x(toT(sb.end)) - g.x(toT(sb.start)), g.ph);
      }
      for (const b of d.bounds || []) if (b.brake) {
        ctx.fillStyle = 'rgba(255,107,107,0.08)';
        ctx.fillRect(g.x(toT(b.start)), M.t, g.x(toT(b.end)) - g.x(toT(b.start)), g.ph);
      }
    }
    this.grid(ctx, g);
    if (d) {
      const fs = d.fs, toT = (i) => i * 1000 / fs;
      if (d.overlay?.float) this.line(ctx, g, d.overlay.float, d.overlay.fs, d.overlay.offset_ms || 0, css('--ref'), 1.2, 0.75);
      if (this.show.sim && d.sim?.acc) {
        this.line(ctx, g, d.sim.acc, fs, 0, css('--sim'), 1.1, 0.9);
        if (d.robust) for (const r of d.robust) this.line(ctx, g, r, fs, 0, css('--sim'), 0.8, 0.35);
      }
      this.samples(ctx, g, d.samples, fs);
      if (this.show.env && d.env) {
        ctx.setLineDash([3, 3]);
        this.line(ctx, g, d.env, fs, 0, css('--env'), 1, 0.8);
        ctx.setLineDash([]);
      }
      ctx.strokeStyle = 'rgba(214,219,227,0.25)';
      for (const b of d.bounds || []) {
        const x = Math.round(g.x(toT(b.end))) + 0.5;
        ctx.beginPath(); ctx.moveTo(x, M.t); ctx.lineTo(x, M.t + g.ph); ctx.stroke();
      }
      const xe = Math.round(g.x(toT(d.samples.length))) + 0.5;
      ctx.strokeStyle = 'rgba(255,255,255,0.5)'; ctx.setLineDash([2, 4]);
      ctx.beginPath(); ctx.moveTo(xe, M.t); ctx.lineTo(xe, M.t + g.ph); ctx.stroke(); ctx.setLineDash([]);
      this.drawHandles(ctx, g);
      if (this.hover) {
        ctx.strokeStyle = 'rgba(255,255,255,0.35)';
        const x = Math.round(this.hover.x) + 0.5;
        ctx.beginPath(); ctx.moveTo(x, M.t); ctx.lineTo(x, M.t + g.ph); ctx.stroke();
      }
    }
    ctx.restore();
    if (d) this.labels(ctx, g);
    this.axes(ctx, g);
  }
  grid(ctx, g) {
    const v = this.view; const step = niceStep(v.t1 - v.t0, g.pw / 90);
    ctx.strokeStyle = css('--grid'); ctx.lineWidth = 1;
    for (let t = Math.ceil(v.t0 / step) * step; t <= v.t1; t += step) {
      const x = Math.round(g.x(t)) + 0.5; ctx.beginPath(); ctx.moveTo(x, M.t); ctx.lineTo(x, M.t + g.ph); ctx.stroke();
    }
    for (const a of [-1, -0.5, 0.5, 1]) {
      const y = Math.round(g.y(a)) + 0.5; ctx.beginPath(); ctx.moveTo(M.l, y); ctx.lineTo(M.l + g.pw, y); ctx.stroke();
    }
    ctx.strokeStyle = '#3a4250';
    const y0 = Math.round(g.y(0)) + 0.5; ctx.beginPath(); ctx.moveTo(M.l, y0); ctx.lineTo(M.l + g.pw, y0); ctx.stroke();
  }
  axes(ctx, g) {
    const v = this.view; const step = niceStep(v.t1 - v.t0, g.pw / 90);
    ctx.fillStyle = css('--muted'); ctx.font = `10px ${css('--mono')}`; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    for (let t = Math.ceil(v.t0 / step) * step; t <= v.t1; t += step) ctx.fillText(fmtT(t, step), g.x(t), M.t + g.ph + 3);
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (const a of [-1, -0.5, 0, 0.5, 1]) ctx.fillText(String(Math.sign(a) * Math.round(Math.abs(a) * 127 * this.yzoom)), M.l - 4, g.y(a));
    ctx.textAlign = 'left'; ctx.fillText('ms', M.l + g.pw - 14, M.t + g.ph + 9);
  }
  labels(ctx, g) {
    const d = this.data; const toT = (i) => i * 1000 / d.fs;
    ctx.font = `10px ${css('--mono')}`; ctx.textBaseline = 'top'; ctx.textAlign = 'left';
    const items = (d.slot_bounds?.length ? d.slot_bounds : d.bounds) || [];
    let lastX = -1e9;
    for (const b of items) {
      const x0 = Math.max(M.l, g.x(toT(b.start))), x1 = Math.min(M.l + g.pw, g.x(toT(b.end)));
      if (x1 - x0 < 18 || x0 < lastX) continue;
      ctx.fillStyle = b.brake ? css('--brake') : css('--muted');
      const txt = b.label ?? '';
      ctx.save(); ctx.beginPath(); ctx.rect(x0, 0, x1 - x0 - 2, M.t); ctx.clip();
      ctx.fillText(txt, x0 + 2, 2); ctx.restore();
      lastX = x0 + Math.min(x1 - x0, ctx.measureText(txt).width + 6);
    }
  }
  line(ctx, g, arr, fs, off, color, lw, alpha) {
    if (!arr?.length) return;
    const v = this.view, n = arr.length, msPer = 1000 / fs;
    const i0 = Math.max(0, Math.floor((v.t0 - off) / msPer) - 1), i1 = Math.min(n - 1, Math.ceil((v.t1 - off) / msPer) + 1);
    if (i1 < i0) return;
    ctx.globalAlpha = alpha; ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.beginPath();
    const pps = g.pw / ((v.t1 - v.t0) / msPer);
    if (pps < 1) {
      // min/max per pixel column, joined into one path so the trace stays continuous
      let px = null, mn = 0, mx = 0, started = false;
      const flush = () => {
        const x = px + 0.5;
        if (!started) { ctx.moveTo(x, g.y(mn)); started = true; } else ctx.lineTo(x, g.y(mn));
        ctx.lineTo(x, g.y(mx));
      };
      for (let i = i0; i <= i1; i++) {
        const x = Math.round(g.x(off + i * msPer));
        if (px === null) { px = x; mn = mx = arr[i]; continue; }
        if (x !== px) { flush(); px = x; mn = mx = arr[i]; } else { mn = Math.min(mn, arr[i]); mx = Math.max(mx, arr[i]); }
      }
      if (px !== null) flush();
    } else {
      for (let i = i0; i <= i1; i++) {
        const x = g.x(off + i * msPer), y = g.y(arr[i]);
        if (i === i0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
    }
    ctx.stroke(); ctx.globalAlpha = 1; ctx.lineWidth = 1;
  }
  samples(ctx, g, s, fs) {
    if (!s?.length) return;
    const v = this.view, msPer = 1000 / fs, pps = g.pw / ((v.t1 - v.t0) / msPer);
    const arr = this._f && this._src === s ? this._f : (this._src = s, this._f = Float32Array.from(s, (x) => x / 127));
    if (pps < 8) { this.line(ctx, g, arr, fs, 0, css('--accent'), 1.3, 1); return; }
    const i0 = Math.max(0, Math.floor(v.t0 / msPer)), i1 = Math.min(arr.length - 1, Math.ceil(v.t1 / msPer));
    ctx.strokeStyle = css('--accent'); ctx.fillStyle = css('--accent'); ctx.globalAlpha = 0.5; ctx.beginPath();
    for (let i = i0; i <= i1; i++) { const x = g.x(i * msPer); ctx.moveTo(x, g.y(0)); ctx.lineTo(x, g.y(arr[i])); }
    ctx.stroke(); ctx.globalAlpha = 1;
    this.line(ctx, g, arr, fs, 0, css('--accent'), 1, 0.6);
    for (let i = i0; i <= i1; i++) { ctx.beginPath(); ctx.arc(g.x(i * msPer), g.y(arr[i]), 2, 0, 7); ctx.fill(); }
  }
  drawHandles(ctx, g) {
    for (const hd of this.handles) {
      const p = this.drag?.h === hd ? this.drag.cur : hd;
      const x = g.x(p.t), y = hd.kind === 'end' ? M.t + g.ph - 5 : g.y(p.v);
      ctx.fillStyle = hd.kind === 'end' ? '#e6e6e6' : (p.v < 0 ? css('--brake') : css('--env'));
      ctx.strokeStyle = '#000';
      if (hd.kind === 'end') { ctx.beginPath(); ctx.moveTo(x, y - 6); ctx.lineTo(x + 5, y + 4); ctx.lineTo(x - 5, y + 4); ctx.closePath(); }
      else { ctx.beginPath(); ctx.rect(x - 4, y - 4, 8, 8); }
      ctx.fill(); ctx.stroke();
    }
  }
  pos(e) { const r = this.c.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
  hit(x, y) {
    const g = this.geom();
    let best = null, bd = 8;
    for (const hd of this.handles) {
      const hx = g.x(hd.t), hy = hd.kind === 'end' ? M.t + g.ph - 5 : g.y(hd.v);
      const dist = Math.hypot(hx - x, hy - y);
      if (dist < bd) { bd = dist; best = hd; }
    }
    return best;
  }
  onWheel(e) {
    e.preventDefault();
    const [x] = this.pos(e); const g = this.geom();
    const f = Math.exp(e.deltaY * 0.0015);
    if (e.shiftKey) { this.yzoom = Math.min(2, Math.max(0.02, this.yzoom * f)); this.request(); return; }
    this.view.zoom(f, g.t(x));
  }
  onDown(e) {
    if (e.button !== 0) return;
    const [x, y] = this.pos(e); const h = this.hit(x, y);
    if (h) { this.drag = { h, cur: { t: h.t, v: h.v } }; e.preventDefault(); return; }
    this.pan = { x, t0: this.view.t0, t1: this.view.t1 };
  }
  onMove(e) {
    const [x, y] = this.pos(e); const g = this.geom();
    if (this.drag) {
      const cur = this.drag.cur;
      if (this.drag.h.kind === 'end') cur.t = g.t(x); else cur.v = Math.max(-1.5, Math.min(1.5, g.a(y)));
      this.hooks.handleDrag?.(this.drag.h, cur, e, false);
      this.request(); return;
    }
    if (this.pan) {
      const dt = (x - this.pan.x) / g.pw * (this.pan.t1 - this.pan.t0);
      this.view.set(this.pan.t0 - dt, this.pan.t1 - dt); return;
    }
    if (x < 0 || y < 0 || x > g.w || y > g.h) return;
    this.hover = { x, y, t: g.t(x), a: g.a(y) };
    this.c.style.cursor = this.hit(x, y) ? (this.hit(x, y).kind === 'end' ? 'ew-resize' : 'ns-resize') : 'crosshair';
    this.hooks.hover?.(this.hover);
    this.request();
  }
  onUp(e) {
    if (this.drag) { const d = this.drag; this.drag = null; this.hooks.handleDrag?.(d.h, d.cur, e, true); this.request(); }
    this.pan = null;
  }
}

const MAGMA = [[0, 0, 0, 4], [0.13, 28, 16, 68], [0.25, 79, 18, 123], [0.38, 129, 37, 129], [0.5, 181, 54, 122],
  [0.63, 229, 80, 100], [0.75, 251, 135, 97], [0.88, 254, 194, 135], [1, 252, 253, 191]];
export const LUT = (() => {
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const u = i / 255; let k = 0;
    while (k < MAGMA.length - 2 && u > MAGMA[k + 1][0]) k++;
    const [u0, ...c0] = MAGMA[k], [u1, ...c1] = MAGMA[k + 1], f = (u - u0) / (u1 - u0);
    for (let j = 0; j < 3; j++) lut[i * 3 + j] = c0[j] + (c1[j] - c0[j]) * f;
  }
  return lut;
})();

export class SpectroPlot extends Base {
  constructor(canvas, view, hooks = {}) {
    super(canvas); this.view = view; this.hooks = hooks; this.sg = null; this.img = null;
    view.on(() => this.request());
    canvas.addEventListener('wheel', (e) => {
      e.preventDefault(); const r = canvas.getBoundingClientRect(); const g = this.geom();
      this.view.zoom(Math.exp(e.deltaY * 0.0015), g.t(e.clientX - r.left));
    }, { passive: false });
    canvas.addEventListener('mousedown', (e) => { if (e.button !== 0) return; const r = canvas.getBoundingClientRect(); this.pan = { x: e.clientX - r.left, t0: view.t0, t1: view.t1 }; });
    window.addEventListener('mousemove', (e) => {
      const r = canvas.getBoundingClientRect(); const x = e.clientX - r.left, y = e.clientY - r.top; const g = this.geom();
      if (this.pan) { const dt = (x - this.pan.x) / g.pw * (this.pan.t1 - this.pan.t0); view.set(this.pan.t0 - dt, this.pan.t1 - dt); return; }
      if (x >= 0 && y >= 0 && x <= g.w && y <= g.h && e.target === canvas) this.hooks.hover?.(this.readout(x, y));
    });
    window.addEventListener('mouseup', () => { this.pan = null; });
    canvas.addEventListener('dblclick', () => view.fit());
  }
  setData(sg, ifreq, f0, fmax) {
    this.sg = sg; this.ifreq = ifreq || []; this.f0 = f0; this.fmax = fmax;
    if (sg) {
      const { nt, nf } = sg; const data = Uint8Array.from(atob(sg.data_b64), (c) => c.charCodeAt(0));
      this.raw = data;
      const off = new OffscreenCanvas(Math.max(1, nt), Math.max(1, nf)); const octx = off.getContext('2d');
      const im = octx.createImageData(Math.max(1, nt), Math.max(1, nf));
      for (let t = 0; t < nt; t++) for (let f = 0; f < nf; f++) {
        const v = data[t * nf + f], o = ((nf - 1 - f) * nt + t) * 4;
        im.data[o] = LUT[v * 3]; im.data[o + 1] = LUT[v * 3 + 1]; im.data[o + 2] = LUT[v * 3 + 2]; im.data[o + 3] = 255;
      }
      octx.putImageData(im, 0, 0); this.img = off;
    } else this.img = null;
    this.request();
  }
  geom() {
    const w = this.c.clientWidth, h = this.c.clientHeight; const pw = Math.max(10, w - M.l - M.r), ph = Math.max(10, h - 6 - M.b);
    const v = this.view; const fmax = this.fmax || 1000;
    return { w, h, pw, ph, x: (t) => M.l + (t - v.t0) / (v.t1 - v.t0) * pw, t: (x) => v.t0 + (x - M.l) / pw * (v.t1 - v.t0),
      y: (f) => 6 + (1 - f / fmax) * ph, f: (y) => (1 - (y - 6) / ph) * fmax };
  }
  readout(x, y) {
    const g = this.geom(); if (!this.sg) return null;
    const t = g.t(x), f = g.f(y); const sg = this.sg;
    const ti = Math.round((t - sg.t0_ms) / sg.dt_ms), fi = Math.round(f / sg.df_hz);
    let db = null;
    if (ti >= 0 && ti < sg.nt && fi >= 0 && fi < sg.nf) db = sg.db_min + this.raw[ti * sg.nf + fi] / 255 * (sg.db_max - sg.db_min);
    return { t, f, db };
  }
  draw() {
    const [w, h] = this.size(); const ctx = this.ctx; const g = this.geom();
    ctx.fillStyle = '#000004'; ctx.fillRect(0, 0, w, h);
    const sg = this.sg;
    if (sg && this.img) {
      const span = sg.nt * sg.dt_ms; const tStart = sg.t0_ms - sg.dt_ms / 2;
      const xa = g.x(tStart), xb = g.x(tStart + span);
      ctx.imageSmoothingEnabled = true;
      const fTop = (sg.nf - 0.5) * sg.df_hz;
      ctx.save(); ctx.beginPath(); ctx.rect(M.l, 6, g.pw, g.ph); ctx.clip();
      ctx.drawImage(this.img, 0, 0, sg.nt, sg.nf, xa, g.y(fTop), xb - xa, g.y(-0.5 * sg.df_hz) - g.y(fTop));
      ctx.fillStyle = '#4cc3ff'; ctx.strokeStyle = '#000';
      for (const [t, f] of this.ifreq) {
        if (f <= this.fmax) { ctx.beginPath(); ctx.arc(g.x(t), g.y(f), 2.2, 0, 7); ctx.fill(); ctx.stroke(); }
      }
      if (this.f0) {
        ctx.strokeStyle = 'rgba(126,231,135,0.7)'; ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(M.l, g.y(this.f0)); ctx.lineTo(M.l + g.pw, g.y(this.f0)); ctx.stroke(); ctx.setLineDash([]);
      }
      ctx.restore();
    }
    ctx.fillStyle = css('--muted'); ctx.font = `10px ${css('--mono')}`; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    const fstep = niceStep(this.fmax || 1000, g.ph / 28);
    for (let f = 0; f <= (this.fmax || 1000); f += fstep) ctx.fillText(String(f), M.l - 4, g.y(f));
    const v = this.view; const step = niceStep(v.t1 - v.t0, g.pw / 90);
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    for (let t = Math.ceil(v.t0 / step) * step; t <= v.t1; t += step) ctx.fillText(fmtT(t, step), g.x(t), 6 + g.ph + 3);
    ctx.textAlign = 'left'; ctx.fillText('Hz', 4, 2);
  }
}

export class SpectrumPlot extends Base {
  constructor(canvas) { super(canvas); this.d = null; }
  setData(spec, f0, q, fmax) { this.d = spec; this.f0 = f0; this.q = q; this.fmax = fmax; this.request(); }
  draw() {
    const [w, h] = this.size(); const ctx = this.ctx; const l = 30, r = 6, t = 16, b = 18;
    const pw = w - l - r, ph = h - t - b, fmax = this.fmax || 1000, dbMin = -60, dbMax = 6;
    const x = (f) => l + f / fmax * pw, y = (db) => t + (1 - (db - dbMin) / (dbMax - dbMin)) * ph;
    ctx.fillStyle = css('--bg'); ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = css('--grid');
    for (let db = -60; db <= 0; db += 20) { ctx.beginPath(); ctx.moveTo(l, y(db) + 0.5); ctx.lineTo(l + pw, y(db) + 0.5); ctx.stroke(); }
    ctx.fillStyle = css('--muted'); ctx.font = `10px ${css('--mono')}`; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (let db = -60; db <= 0; db += 20) ctx.fillText(String(db), l - 3, y(db));
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    const fs = niceStep(fmax, pw / 50);
    for (let f = 0; f <= fmax; f += fs) ctx.fillText(String(f), x(f), t + ph + 3);
    ctx.textAlign = 'left'; ctx.fillText('spectrum dB', l, 2);
    if (this.f0 && this.q) {
      ctx.strokeStyle = 'rgba(126,231,135,0.5)'; ctx.setLineDash([3, 3]); ctx.beginPath();
      for (let i = 0; i <= pw; i++) {
        const f = i / pw * fmax, rr = f / this.f0;
        const hm = rr * rr / Math.sqrt((1 - rr * rr) ** 2 + (rr / this.q) ** 2) / this.q;
        const yy = y(20 * Math.log10(hm + 1e-9));
        if (i === 0) ctx.moveTo(l + i, yy); else ctx.lineTo(l + i, yy);
      }
      ctx.stroke(); ctx.setLineDash([]);
      ctx.strokeStyle = 'rgba(126,231,135,0.8)'; ctx.beginPath(); ctx.moveTo(x(this.f0), t); ctx.lineTo(x(this.f0), t + ph); ctx.stroke();
    }
    const d = this.d; if (!d?.f?.length) return;
    ctx.save(); ctx.beginPath(); ctx.rect(l, t, pw, ph); ctx.clip();
    ctx.strokeStyle = css('--accent'); ctx.lineWidth = 1.2; ctx.beginPath();
    d.f.forEach((f, i) => { const xx = x(f), yy = y(d.db[i]); if (i === 0) ctx.moveTo(xx, yy); else ctx.lineTo(xx, yy); });
    ctx.stroke(); ctx.restore();
    let im = 0; d.db.forEach((v, i) => { if (v > d.db[im]) im = i; });
    ctx.fillStyle = css('--text'); ctx.textAlign = 'right'; ctx.textBaseline = 'top';
    ctx.fillText(`${d.f[im].toFixed(0)} Hz ${d.db[im].toFixed(1)} dB`, w - 4, 2);
  }
}
