// Headless UI smoke test over CDP (Node 22, no npm deps).
// usage: node tools/uitest.mjs [base_url] [out_dir] [WxH]
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';

const BASE = process.argv[2] || 'http://127.0.0.1:8771/';
const OUT = process.argv[3] || new URL('../out/ui/', import.meta.url).pathname;
const [W, H] = (process.argv[4] || '1680x1000').split('x').map(Number);
const CHROME = process.env.CHROME || `${process.env.HOME}/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell`;
const PORT = 9300 + Math.floor(Math.random() * 500);
mkdirSync(OUT, { recursive: true });

const chrome = spawn(CHROME, ['--no-sandbox', '--disable-gpu', '--hide-scrollbars', `--remote-debugging-port=${PORT}`,
  `--window-size=${W},${H}`, 'about:blank'], { stdio: ['ignore', 'ignore', 'ignore'] });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws, seq = 0; const pending = new Map(); const errors = []; const consoleLines = [];

async function connect() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((t) => t.type === 'page');
      if (page) return page.webSocketDebuggerUrl;
    } catch { /* starting */ }
    await sleep(100);
  }
  throw new Error('chrome did not start');
}
function send(method, params = {}) {
  const id = ++seq; ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject, method }));
}
async function evaluate(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(`${expr}: ${r.exceptionDetails.exception?.description || r.exceptionDetails.text}`);
  return r.result.value;
}
async function shot(name) {
  const r = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(`${OUT}/${name}.png`, Buffer.from(r.data, 'base64'));
  console.log(`shot ${OUT}/${name}.png`);
}
async function waitFor(expr, ms = 8000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (await evaluate(expr).catch(() => false)) return; await sleep(80); }
  throw new Error(`timeout waiting for ${expr}`);
}
const idle = () => waitFor('(() => { const h = window.__hapkit; return h && !document.querySelector("#metrics .err") && h.S.render; })()');
async function mouse(type, x, y, extra = {}) { await send('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1, ...extra }); }

async function main() {
  ws = new WebSocket(await connect());
  await new Promise((r) => ws.addEventListener('open', r, { once: true }));
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(`${p.method}: ${m.error.message}`)) : p.resolve(m.result); return; }
    if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    if (m.method === 'Runtime.consoleAPICalled') {
      const text = m.params.args.map((a) => a.value ?? a.description).join(' ');
      consoleLines.push(`${m.params.type}: ${text}`); if (m.params.type === 'error') errors.push(text);
    }
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') errors.push(`${m.params.entry.text} ${m.params.entry.url || ''}`);
  });
  await send('Runtime.enable'); await send('Log.enable'); await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: BASE });
  await waitFor('window.__hapkit && window.__hapkit.ready');
  await idle(); await sleep(400);
  await shot('01_start');

  const waves = await evaluate('window.__hapkit.S.project.waves.map(w => w.id)');
  await evaluate(`window.__hapkit.select('waves', ${JSON.stringify(waves[0])})`);
  await sleep(600); await shot('02_wave');

  // new parametric wave, drag its first amp handle, zoom
  await evaluate('document.querySelector("#lib h3 button").click()');
  await sleep(700); await shot('03_newwave');
  const box = await evaluate('(() => { const r = document.getElementById("wavCanvas").getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()');
  const handle = await evaluate(`(() => { const p = window.__hapkit; return null; })()`);
  void handle;
  await mouse('mouseMoved', box[0] + box[2] * 0.5, box[1] + box[3] * 0.5);
  await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: box[0] + box[2] * 0.3, y: box[1] + box[3] * 0.5, deltaX: 0, deltaY: -300 });
  await sleep(300); await shot('04_zoom');

  const seqs = await evaluate('window.__hapkit.S.project.sequences.map(s => s.id)');
  if (seqs.length) { await evaluate(`window.__hapkit.select('sequences', ${JSON.stringify(seqs[1] || seqs[0])})`); await sleep(700); await shot('05_sequence'); }
  const effs = await evaluate('window.__hapkit.S.project.effects.filter(e => e.enabled).map(e => e.id)');
  if (effs.length) { await evaluate(`window.__hapkit.select('effects', ${JSON.stringify(effs[0])})`); await sleep(700); await shot('06_effect'); }

  console.log(consoleLines.join('\n'));
  if (errors.length) { console.error('ERRORS:\n' + errors.join('\n')); process.exitCode = 1; } else console.log('no console errors');
}

main().catch((e) => { console.error(e); process.exitCode = 2; }).finally(() => { try { ws?.close(); } catch { /* */ } chrome.kill(); });
