// Tab "Console": WebSocket log, send raw G-code lines, copy.
import { h, copyText, lsGet, lsSet } from '../util.js';
import { icon } from '../icons.js';
import { store, viewing } from '../store.js';
import { api } from '../api.js';
import { run, gate, toast } from '../ui.js';
import { caps } from '../caps.js';

const LEVELS = [['tx', 'Sent'], ['rx', 'Received'], ['info', 'Info'], ['warn', 'Warning'], ['error', 'Error']];
const TAG = { tx: 'TX', rx: 'RX', info: 'INF', warn: 'WRN', error: 'ERR' };
const MAX_DOM = 1500;

export const hhmmss = (t) => new Date((t || Date.now() / 1000) * 1000).toLocaleTimeString('en-GB', { hour12: false });

export function mountConsole(root) {
  const hidden = new Set(lsGet('wbot-log-hide', []));
  let auto = true, rendered = 0;
  const log = h('div', { class: 'log', role: 'log', 'aria-live': 'off', tabindex: '0', 'aria-label': 'Log' });

  const filters = h('div', { class: 'row', style: { alignItems: 'center' } }, LEVELS.map(([lv, label]) => {
    const b = h('button', { class: 'btn sm', type: 'button', 'aria-pressed': String(!hidden.has(lv)),
      onClick: () => { hidden.has(lv) ? hidden.delete(lv) : hidden.add(lv); b.setAttribute('aria-pressed', String(!hidden.has(lv))); apply(); lsSet('wbot-log-hide', [...hidden]); } }, label);
    return b;
  }));
  const autoBtn = h('button', { class: 'btn sm', type: 'button', 'aria-pressed': 'true', onClick: () => { auto = !auto; autoBtn.setAttribute('aria-pressed', String(auto)); if (auto) log.scrollTop = log.scrollHeight; } }, 'Follow');
  const copyBtn = h('button', { class: 'btn sm', type: 'button' }, icon('copy'), 'Copy');
  const clearBtn = h('button', { class: 'btn sm', type: 'button' }, icon('trash'), 'Clear');
  copyBtn.addEventListener('click', async () => {
    const txt = store.get('logs').filter((r) => !hidden.has(r.level)).map((r) => `[${hhmmss(r.t)}] ${TAG[r.level] ?? r.level} ${r.text}`).join('\n');
    toast((await copyText(txt)) ? `${txt ? txt.split('\n').length : 0} lines copied.` : 'Copy not available. Select the text in the log and press Ctrl+C.', 'info');
  });
  clearBtn.addEventListener('click', () => { store.get('logs').length = 0; log.replaceChildren(); rendered = store.get('logSeq') ?? 0; });

  function apply() { log.dataset.hide = [...hidden].join(' '); }
  apply();

  function line(r) {
    return h('div', { class: 'll ' + r.level }, h('span', { class: 't' }, hhmmss(r.t)), h('span', { class: 'k' }, TAG[r.level] ?? r.level), h('span', { class: 'm' }, r.text));
  }
  store.on('logs', () => {
    const logs = store.get('logs');
    const stick = auto && log.scrollHeight - log.scrollTop - log.clientHeight < 60 || auto;
    const frag = document.createDocumentFragment();
    for (const r of logs) if (r.n > rendered) { frag.append(line(r)); rendered = r.n; }
    if (!frag.childNodes.length) return;
    log.append(frag);
    while (log.childElementCount > MAX_DOM) log.firstChild.remove();
    if (stick) log.scrollTop = log.scrollHeight;
  }, { immediate: true });

  store.on(['tab', 'moreOpen'], () => { if (viewing('konsole') && auto) requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; }); });

  /* ---------- Input ---------- */
  const history = lsGet('wbot-gcode-hist', []);
  let hi = history.length;
  const input = h('input', { type: 'text', placeholder: 'G-code or $ command, e.g. ?  $I  $G', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false',
    style: { fontFamily: 'var(--font-mono)' }, 'aria-label': 'Raw command' });
  const sendBtn = h('button', { class: 'btn primary', type: 'button' }, icon('send'), 'Send');
  const note = h('div', { class: 'gate-note' });

  async function send(text) {
    const line = (text ?? input.value).trim();
    if (!line) return;
    const r = await api.gcode(line);
    if (history.at(-1) !== line) { history.push(line); if (history.length > 50) history.shift(); lsSet('wbot-gcode-hist', history); }
    hi = history.length; input.value = '';
    if (store.get('ws') !== 'open') {                        // no live log: show the lines ourselves
      const seq = (store.get('logSeq') ?? 0);
      const logs = store.get('logs');
      logs.push({ n: seq + 1, level: 'tx', text: line, t: Date.now() / 1000 });
      (r?.replies ?? []).forEach((x, i) => logs.push({ n: seq + 2 + i, level: 'rx', text: x, t: Date.now() / 1000 }));
      store.set({ logSeq: seq + 1 + (r?.replies?.length ?? 0) }); store.touch('logs');
    }
  }
  sendBtn.addEventListener('click', () => run(sendBtn, () => send()));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); run(sendBtn, () => send()); }
    else if (e.key === 'ArrowUp' && history.length) { e.preventDefault(); hi = Math.max(0, hi - 1); input.value = history[hi] ?? ''; }
    else if (e.key === 'ArrowDown') { e.preventDefault(); hi = Math.min(history.length, hi + 1); input.value = history[hi] ?? ''; }
  });
  const quick = h('div', { class: 'row' }, ['?', '$I', '$G'].map((q) => {
    const b = h('button', { class: 'btn sm', type: 'button', onClick: () => run(b, () => send(q)) }, q);
    b._q = true; return b;
  }));

  root.classList.add('panel-console');
  root.append(
    h('div', { class: 'sect' }, h('div', { class: 'sect-h' }, h('h2', null, 'Log'),
      h('div', { class: 'row' }, autoBtn, copyBtn, clearBtn)), filters, log),
    h('div', { class: 'sect' }, h('div', { class: 'sect-h' }, h('h2', null, 'Send raw command')),
      h('div', { class: 'row' }, h('div', { class: 'field grow' }, input), sendBtn), quick, note,
      h('p', { class: 'field-hint' }, 'Raw commands go straight to the firmware. Send G-code that moves the gondola only with the position reference set, and with care.')));

  store.on(['state'], () => {
    const c = caps(store.get('state'), store.get('job')), why = c.connected ? '' : c.online;
    gate(sendBtn, why); input.disabled = !c.connected;
    quick.querySelectorAll('button').forEach((b) => gate(b, why));
    note.textContent = why;
  }, { immediate: true });
}
