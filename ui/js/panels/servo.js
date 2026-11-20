// Reiter "Servos": Stift-Servo und Revolver direkt testen, Impulsbreite anzeigen, Sequenztests.
import { h, fmt, throttle, sleep } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, gate, toast, reportError } from '../ui.js';
import { numField, slider, toggle, swatch } from '../widgets.js';
import { caps } from '../caps.js';

export const pulseUs = (spec, deg) => (spec?.center_us ?? 1500) + (spec?.us_per_deg ?? 5.5556) * deg;

/** Sendet immer nur den neuesten Wert; solange eine Anfrage läuft, wird gesammelt. */
function latest(fn) {
  let busy = false, pending = null;
  async function send(v) {
    if (busy) { pending = v; return; }
    busy = true;
    try { await fn(v); } catch (e) { reportError(e); }
    busy = false;
    if (pending != null) { const p = pending; pending = null; send(p); }
  }
  return send;
}

const q = (v, [lo, hi]) => Math.min(hi, Math.max(lo, Math.round(v * 2) / 2));

export function mountServo(root) {
  const sect = (title, ...kids) => h('div', { class: 'sect' }, h('div', { class: 'sect-h' }, h('h2', null, title)), ...kids);
  const note = () => h('div', { class: 'gate-note', role: 'status' });
  let seqRunning = false, seqStop = false;

  /* ---------- Stift ---------- */
  const penPulse = h('div', { class: 'pulse' });
  const penSl = slider({ min: -44, max: 44, step: 'any', value: 0, label: 'Stift-Winkel' });
  const penNum = numField({ label: 'Winkel', unit: '°', step: 0.5, value: 0, cls: 'angle-in' });
  const penLive = toggle({ label: 'Live beim Ziehen', checked: true });
  const penIs = h('div', { class: 'field-hint' });
  const penNote = note();
  const sendPen = latest((a) => api.pen(a));
  const livePen = throttle(sendPen, 120);
  const setPen = (v) => { penSl.input.value = String(v); penNum.input.value = String(v); paintPulse(); };
  const pv = () => q(penSl.get(), limits().pen), rv = () => q(revSl.get(), limits().revolver);
  penSl.input.addEventListener('input', () => { const v = pv(); penNum.input.value = String(v); paintPulse(); if (penLive.get()) livePen(v); });
  penSl.input.addEventListener('change', () => { livePen.cancel(); sendPen(pv()); });
  for (const ev of ['pointerdown']) penSl.input.addEventListener(ev, () => { penSl.input._drag = true; });
  window.addEventListener('pointerup', () => { penSl.input._drag = false; revSl.input._drag = false; });
  penNum.input.addEventListener('change', () => {
    const v = penNum.get(); if (v == null) return;
    const [lo, hi] = limits().pen; const c = Math.min(hi, Math.max(lo, v));
    setPen(c); sendPen(c);
  });
  const penPresets = h('div', { class: 'row' });
  const penSend = h('button', { class: 'btn', type: 'button' }, icon('send'), 'Winkel senden');
  penSend.addEventListener('click', () => run(penSend, () => api.pen(pv())));

  /* ---------- Revolver ---------- */
  const revPulse = h('div', { class: 'pulse' });
  const revSl = slider({ min: -90, max: 90, step: 'any', value: 0, label: 'Revolver-Winkel' });
  const revNum = numField({ label: 'Winkel', unit: '°', step: 0.5, value: 0, cls: 'angle-in' });
  const revSend = h('button', { class: 'btn', type: 'button' }, icon('send'), 'Winkel senden');
  const slotWrap = h('div', { class: 'grid3' });
  const revIs = h('div', { class: 'field-hint' });
  const revNote = note();
  revSl.input.addEventListener('input', () => { revNum.input.value = String(rv()); paintPulse(); });
  revSl.input.addEventListener('pointerdown', () => { revSl.input._drag = true; });
  revSend.addEventListener('click', () => run(revSend, () => api.revolver({ angle: rv() })));
  revSl.input.addEventListener('change', () => run(revSend, () => api.revolver({ angle: rv() })));
  revNum.input.addEventListener('change', () => {
    const v = revNum.get(); if (v == null) return;
    const [lo, hi] = limits().revolver; const c = Math.min(hi, Math.max(lo, v));
    revSl.input.value = String(c); revNum.input.value = String(c); paintPulse();
    run(revSend, () => api.revolver({ angle: c }));
  });

  /* ---------- Sequenztests ---------- */
  const nF = numField({ label: 'Wiederholungen', min: 1, max: 100, step: 1, value: 10 });
  const pauseF = numField({ label: 'Pause je Schritt', unit: 's', min: 0, max: 10, step: 0.1, value: 0.4 });
  const penSeq = h('button', { class: 'btn', type: 'button' }, 'Stift heben und senken');
  const colSeq = h('button', { class: 'btn', type: 'button' }, 'Farben durchschalten');
  const stopSeq = h('button', { class: 'btn danger', type: 'button', hidden: true }, icon('stop'), 'Test stoppen');
  const seqInfo = h('div', { class: 'muted', role: 'status' });
  const seqNote = note();
  async function sequence(btn, label, steps) {
    seqRunning = true; seqStop = false; stopSeq.hidden = false; update();
    try {
      for (let i = 0; i < steps.length && !seqStop; i++) {
        seqInfo.textContent = `${label}: Schritt ${i + 1} von ${steps.length}`;
        await steps[i]();
        await sleep((pauseF.get() ?? 0.4) * 1000);
      }
      seqInfo.textContent = seqStop ? `${label}: gestoppt.` : `${label}: fertig.`;
    } catch (e) {
      seqInfo.textContent = `${label}: abgebrochen.`;
      reportError(e);
    } finally { seqRunning = false; stopSeq.hidden = true; update(); }
  }
  penSeq.addEventListener('click', () => {
    const n = Math.max(1, Math.round(nF.get() ?? 10)), steps = [];
    for (let i = 0; i < n; i++) steps.push(() => api.pen('down'), () => api.pen('up'));
    sequence(penSeq, `Stift ${n}×`, steps);
  });
  colSeq.addEventListener('click', () => {
    const n = Math.max(1, Math.round(nF.get() ?? 10)), slots = store.get('config')?.colors?.length ?? 3, steps = [];
    for (let i = 0; i < Math.max(1, Math.round(n / slots)); i++) for (let s = 1; s <= slots; s++) steps.push(() => api.revolver({ slot: s }));
    sequence(colSeq, 'Farben', steps);
  });
  stopSeq.addEventListener('click', () => { seqStop = true; seqInfo.textContent = 'Stoppe nach dem laufenden Schritt …'; });

  root.append(
    sect('Stift-Servo (Servo B, Achse Z)',
      h('div', { class: 'row', style: { alignItems: 'center' } }, penPulse, h('span', { class: 'grow' }), penNum.el),
      penSl.input, penPresets, penLive.el, penSend, penIs, penNote),
    sect('Revolver (Servo A, Achse A)',
      h('div', { class: 'note warn' }, 'Der Stift wird vor dem Drehen des Revolvers gehoben. Der Revolver bewegt sich nie bei gesenktem Stift.'),
      slotWrap,
      h('div', { class: 'row', style: { alignItems: 'center' } }, revPulse, h('span', { class: 'grow' }), revNum.el),
      revSl.input, revSend, revIs, revNote),
    sect('Sequenztests',
      h('div', { class: 'grid2' }, nF.el, pauseF.el),
      h('div', { class: 'grid2' }, penSeq, colSeq), stopSeq, seqInfo, seqNote,
      h('p', { class: 'field-hint' }, 'Die Servos melden keine Position zurück. Angezeigt werden Sollwerte, die zuletzt gesendet wurden.')));

  const limits = () => {
    const l = store.get('state')?.limits;
    const sp = store.get('config')?.servos;
    return { pen: l?.pen ?? (sp ? [sp.pen.min_deg, sp.pen.max_deg] : [-44, 44]), revolver: l?.revolver ?? (sp ? [sp.revolver.min_deg, sp.revolver.max_deg] : [-90, 90]) };
  };

  function paintPulse() {
    const sv = store.get('config')?.servos;
    const p = pulseUs(sv?.pen, q(parseFloat(penSl.input.value) || 0, limits().pen)), r = pulseUs(sv?.revolver, q(parseFloat(revSl.input.value) || 0, limits().revolver));
    penPulse.replaceChildren(fmt(p, 0), h('small', null, 'µs'));
    revPulse.replaceChildren(fmt(r, 0), h('small', null, 'µs'));
  }

  let presetSig = '', slotSig = '';
  function update() {
    const s = store.get('state'), cfg = store.get('config'), c = caps(s, store.get('job'));
    const reason = c.servo || (seqRunning ? 'Ein Sequenztest läuft.' : '');
    const lim = limits(), sv = cfg?.servos;
    penSl.setLimits(lim.pen[0], lim.pen[1]); revSl.setLimits(lim.revolver[0], lim.revolver[1]);
    penNum.setLimits(lim.pen[0], lim.pen[1]); revNum.setLimits(lim.revolver[0], lim.revolver[1]);
    if (c.connected && s.pen && !penSl.input._drag) { penSl.set(s.pen.angle); penNum.set(s.pen.angle); }
    if (c.connected && s.revolver && !revSl.input._drag) { revSl.set(s.revolver.angle); revNum.set(s.revolver.angle); }
    paintPulse();

    const sig = JSON.stringify([sv?.pen?.up_deg, sv?.pen?.down_deg]);
    if (sig !== presetSig && sv) {
      presetSig = sig;
      penPresets.replaceChildren(...[['Oben', sv.pen.up_deg], ['Unten', sv.pen.down_deg]].map(([n, a]) =>
        h('button', { class: 'btn', type: 'button', dataset: { preset: 1 }, onClick: (e) => run(e.currentTarget, async () => { setPen(a); await api.pen(a); }) },
          `${n} (${fmt(a, 0)}°)`)));
    }
    penPresets.querySelectorAll('button').forEach((b) => gate(b, reason));
    const scol = JSON.stringify([cfg?.colors, sv?.revolver?.slots_deg]);
    if (scol !== slotSig && cfg) {
      slotSig = scol;
      slotWrap.replaceChildren(...cfg.colors.map((col, i) => {
        const b = h('button', { class: 'btn color-btn', type: 'button', dataset: { slot: i + 1 } }, swatch(col.hex),
          h('span', { class: 'nm' }, `${i + 1} · ${col.name}`), h('span', { class: 'faint', style: { fontSize: '12px' } }, `${fmt(sv.revolver.slots_deg[i], 0)}°`));
        b.addEventListener('click', () => run(b, () => api.revolver({ slot: i + 1 })));
        return b;
      }));
    }
    [...slotWrap.children].forEach((b) => { gate(b, reason); b.setAttribute('aria-pressed', String(c.connected && s.revolver?.slot === Number(b.dataset.slot))); });

    for (const b of [penSend, revSend, penSeq, colSeq]) gate(b, reason);
    gate(nF.input, ''); penSl.input.disabled = !!reason; penNum.input.disabled = !!reason; revSl.input.disabled = !!reason; revNum.input.disabled = !!reason;
    penNote.textContent = reason; revNote.textContent = reason; seqNote.textContent = reason;
    if (sv) {
      penIs.textContent = c.connected ? `Zuletzt gesendet: ${fmt(s.pen?.angle, 1)}° · Achse Z = ${fmt(s.position?.axes?.Z, 1)} · Mitte ${fmt(sv.pen.center_us, 0)} µs, ${fmt(sv.pen.us_per_deg, 3)} µs/°. Grenzen ${fmt(lim.pen[0], 1)}…${fmt(lim.pen[1], 1)}°.` : '';
      revIs.textContent = c.connected ? `Zuletzt gesendet: ${fmt(s.revolver?.angle, 1)}° · Achse A = ${fmt(s.position?.axes?.A, 1)} · Mitte ${fmt(sv.revolver.center_us, 0)} µs, ${fmt(sv.revolver.us_per_deg, 3)} µs/°. Grenzen ${fmt(lim.revolver[0], 0)}…${fmt(lim.revolver[1], 0)}°.` : '';
    }
  }
  store.on(['state', 'config', 'job'], update, { immediate: true });
}
