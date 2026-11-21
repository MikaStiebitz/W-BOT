// Shared UI mechanics: notices, confirm dialog, disabling buttons with a reason, double-click protection.
import { h, ApiError } from './util.js';

/* ---------- Notices ----------
   One message per key (same key = replace, never stack), at most 3 visible, always dismissible.
   Errors stay until dismissed or until they time out; the same text is never shown twice. */
const MAX_NOTICES = 3;
const notices = new Map();   // key -> {el, text, timer}

export function notice(key, text, kind = 'info', { ms } = {}) {
  const box = document.getElementById('toasts');
  if (!box || !text) return;
  const life = ms ?? (kind === 'error' ? 15000 : kind === 'warn' ? 9000 : 3200);
  const old = notices.get(key);
  if (old && old.el.isConnected) {
    if (old.text === text && old.kind === kind) { clearTimeout(old.timer); old.timer = life ? setTimeout(() => clearNotice(key), life) : null; return; }
    clearNotice(key);
  }
  const close = h('button', { class: 'toast-x', type: 'button', 'aria-label': 'Dismiss', title: 'Dismiss', onClick: () => clearNotice(key) }, '×');
  const el = h('div', { class: 'toast ' + kind, role: kind === 'error' ? 'alert' : 'status' }, h('span', { class: 'toast-t' }, text), close);
  const rec = { el, text, kind, timer: life ? setTimeout(() => clearNotice(key), life) : null };
  notices.set(key, rec);
  box.append(el);
  while (box.children.length > MAX_NOTICES) {               // drop the oldest one
    const first = [...notices.entries()].find(([, r]) => r.el === box.firstChild);
    if (first) clearNotice(first[0]); else box.firstChild.remove();
  }
}

export function clearNotice(key) {
  const r = notices.get(key);
  if (!r) return;
  clearTimeout(r.timer);
  r.el.remove();
  notices.delete(key);
}

/** Compatible helper: the text is the key, so the same message never appears twice. */
export function toast(text, kind = 'info', ms) { notice(kind + '|' + text, text, kind, ms == null ? {} : { ms }); }

/* ---------- Confirm dialog ---------- */
export function confirmDialog({ title, text, ok = 'OK', cancel = 'Cancel', danger = false }) {
  return new Promise((resolve) => {
    const dlg = h('dialog', null,
      h('h2', null, title),
      h('p', { class: 'muted' }, text),
      h('div', { class: 'actions' },
        h('button', { class: 'btn', type: 'button', onClick: () => dlg.close('no') }, cancel),
        h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), type: 'button', onClick: () => dlg.close('yes') }, ok)));
    dlg.addEventListener('close', () => { resolve(dlg.returnValue === 'yes'); dlg.remove(); });
    document.body.append(dlg);
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
  });
}

/** Dialog with several answers. choices: [{value, label, kind?}] -> the chosen value, or 'cancel' on Esc / click outside. */
export function choiceDialog({ title, text, choices }) {
  return new Promise((resolve) => {
    const dlg = h('dialog', null,
      h('h2', null, title),
      h('p', { class: 'muted' }, text),
      h('div', { class: 'actions' },
        ...choices.map((c) => h('button', { class: 'btn ' + (c.kind ?? ''), type: 'button', onClick: () => dlg.close(c.value) }, c.label))));
    dlg.addEventListener('close', () => { resolve(dlg.returnValue || 'cancel'); dlg.remove(); });
    document.body.append(dlg);
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
  });
}

/* ---------- Disabling with a reason ---------- */
function sync(el) {
  const dis = !!el._reason || !!el._busy;
  el.disabled = dis;
  el.setAttribute('aria-disabled', dis ? 'true' : 'false');
  if (el._reason) el.title = el._reason;
  else if (el.dataset.title) el.title = el.dataset.title;
  else el.removeAttribute('title');
}

/** Disables (reason = text) or enables (reason = '' / null) a control. */
export function gate(el, reason) {
  const r = reason || '';
  if (el._reason === r) return;
  el._reason = r;
  sync(el);
}

/** Shows the reason as readable text (also on touch devices without tooltips). */
export function gateNote(noteEl, reason) {
  noteEl.textContent = reason || '';
}

/** Runs fn, disables the button meanwhile (no double click), catches every error. */
export async function run(btn, fn) {
  if (btn) {
    if (btn._busy) return undefined;
    btn._busy = true;
    btn.classList.add('is-busy');
    sync(btn);
  }
  try {
    return await fn();
  } catch (e) {
    reportError(e);
    return undefined;
  } finally {
    if (btn) {
      btn._busy = false;
      btn.classList.remove('is-busy');
      sync(btn);
    }
  }
}

export function reportError(e) {
  if (e instanceof ApiError) return;     // api.js already showed the notice
  console.error(e);
  notice('unexpected', 'Something went wrong: ' + (e && e.message ? e.message : e), 'error');
}
