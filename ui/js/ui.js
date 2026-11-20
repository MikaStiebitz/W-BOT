// Gemeinsame UI-Mechanik: Toasts, Bestätigungsdialog, Button-Sperren mit Begründung, Doppelklick-Schutz.
import { h, ApiError } from './util.js';

/* ---------- Toasts ---------- */
const recent = new Map();   // text -> {el, n, timer}

export function toast(text, kind = 'info', ms = kind === 'error' ? 7000 : 3200) {
  const box = document.getElementById('toasts');
  if (!box || !text) return;
  const key = kind + '|' + text;
  const hit = recent.get(key);
  if (hit && hit.el.isConnected) {          // gleiche Meldung: zählen statt stapeln
    hit.n += 1;
    hit.count.textContent = `×${hit.n}`;
    clearTimeout(hit.timer);
    hit.timer = setTimeout(() => close(key), ms);
    return;
  }
  const count = h('span', { class: 'count' });
  const el = h('div', { class: 'toast ' + kind, role: kind === 'error' ? 'alert' : 'status', onClick: () => close(key) },
    h('span', { style: { flex: '1' } }, text), count);
  const rec = { el, n: 1, count, timer: setTimeout(() => close(key), ms) };
  recent.set(key, rec);
  box.append(el);
  while (box.children.length > 4) box.firstChild.remove();
}
function close(key) {
  const r = recent.get(key);
  if (!r) return;
  clearTimeout(r.timer);
  r.el.remove();
  recent.delete(key);
}

/* ---------- Bestätigungsdialog ---------- */
export function confirmDialog({ title, text, ok = 'OK', cancel = 'Abbrechen', danger = false }) {
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

/* ---------- Sperren mit Begründung ---------- */
function sync(el) {
  const dis = !!el._reason || !!el._busy;
  el.disabled = dis;
  el.setAttribute('aria-disabled', dis ? 'true' : 'false');
  if (el._reason) el.title = el._reason;
  else if (el.dataset.title) el.title = el.dataset.title;
  else el.removeAttribute('title');
}

/** Sperrt (reason = Text) oder entsperrt (reason = '' / null) ein Bedienelement. */
export function gate(el, reason) {
  const r = reason || '';
  if (el._reason === r) return;
  el._reason = r;
  sync(el);
}

/** Zeigt die Sperr-Begründung als lesbaren Text (auch für Touch ohne Tooltip). */
export function gateNote(noteEl, reason) {
  noteEl.textContent = reason || '';
}

/** Führt fn aus, sperrt den Button währenddessen (kein Doppelklick), fängt alle Fehler. */
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
  if (e instanceof ApiError) return;     // api.js hat schon einen Toast gezeigt
  console.error(e);
  toast('Unerwarteter Fehler: ' + (e && e.message ? e.message : e), 'error');
}
