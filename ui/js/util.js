// Kleine Helfer: DOM-Builder, Formatierung, Timing. Keine Abhängigkeiten.

export class ApiError extends Error {
  constructor(message, code = 'error', status = 0) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
  }
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

const nfCache = new Map();
export function fmt(n, d = 1) {
  if (n == null || typeof n !== 'number' || !Number.isFinite(n)) return '–';
  let f = nfCache.get(d);
  if (!f) {
    f = new Intl.NumberFormat('de-DE', { minimumFractionDigits: d, maximumFractionDigits: d, useGrouping: false });
    nfCache.set(d, f);
  }
  return f.format(Object.is(n, -0) ? 0 : n);
}

export function fmtDur(s) {
  if (s == null || !Number.isFinite(s)) return '–';
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const p = (x) => String(x).padStart(2, '0');
  return h ? `${h}:${p(m)}:${p(sec)} h` : `${m}:${p(sec)} min`;
}

export function debounce(fn, ms) {
  let t = null;
  const f = (...a) => { clearTimeout(t); t = setTimeout(() => { t = null; fn(...a); }, ms); };
  f.cancel = () => { clearTimeout(t); t = null; };
  return f;
}

/** Leading + trailing throttle. flush() sendet sofort den letzten Wert. */
export function throttle(fn, ms) {
  let last = 0, timer = null, pending = null;
  const run = () => { timer = null; last = Date.now(); const a = pending; pending = null; if (a) fn(...a); };
  const f = (...a) => {
    pending = a;
    const wait = ms - (Date.now() - last);
    if (wait <= 0 && !timer) run();
    else if (!timer) timer = setTimeout(run, wait);
  };
  f.cancel = () => { clearTimeout(timer); timer = null; pending = null; };
  return f;
}

/** DOM-Builder: h('div', {class:'x', onClick: fn}, 'text', child, [children]) */
const PROPS = new Set(['value', 'checked', 'disabled', 'selected', 'indeterminate', 'textContent']);
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (PROPS.has(k)) el[k] = v;
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  append(el, kids);
  return el;
}

export function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.firstChild.remove(); return el; }

export function isTyping(t) {
  if (!t || !t.tagName) return false;
  const tag = t.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT' || t.isContentEditable) return true;
  if (tag === 'INPUT') return !['button', 'checkbox', 'radio', 'submit'].includes(t.type);
  return false;
}

export function lsGet(key, fallback = null) {
  try { const v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; }
}
export function lsSet(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); } catch { /* privat/blockiert */ } }

/** Kopieren, auch auf unsicheren Origins (Tablet im WLAN, http://). */
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* weiter mit Fallback */ }
  const ta = h('textarea', { style: { position: 'fixed', left: '-9999px', top: '0' }, 'aria-hidden': 'true' });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

export function downloadBlob(text, filename, type = 'text/plain') {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = h('a', { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** Wert aus verschachteltem Objekt per Pfad "a.b.c". */
export function getPath(o, path) { return path.split('.').reduce((a, k) => (a == null ? undefined : a[k]), o); }
export function setPath(o, path, v) {
  const ks = path.split('.');
  let a = o;
  for (const k of ks.slice(0, -1)) a = a[k] ??= {};
  a[ks.at(-1)] = v;
  return o;
}
