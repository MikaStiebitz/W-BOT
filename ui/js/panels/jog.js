// Reiter "Fahren": Referenz, Jog-Pad, Gehe zu, Park, Stift, Farben.
import { h, fmt, isTyping, lsSet } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, gate, toast } from '../ui.js';
import { ApiError } from '../util.js';
import { numField, segmented, swatch } from '../widgets.js';
import { caps } from '../caps.js';

const STEPS = [1, 5, 10, 50, 100];

export function mountJog(root) {
  const sect = (title, ...kids) => h('div', { class: 'sect' }, h('div', { class: 'sect-h' }, h('h2', null, title)), ...kids);
  const note = () => h('div', { class: 'gate-note', role: 'status' });

  /* ---------- Referenz ---------- */
  const refState = h('div', { class: 'note warn' });
  const rx = numField({ label: 'Gondel steht bei X', unit: 'mm', step: 0.1, value: 750 });
  const ry = numField({ label: 'Y', unit: 'mm', step: 0.1, value: 500 });
  const presets = h('div', { class: 'row', style: { alignItems: 'center' } });
  const refBtn = h('button', { class: 'btn', type: 'button' }, icon('target'), 'Gondel steht jetzt hier');
  const bootBtn = h('button', { class: 'btn primary', type: 'button' }, icon('home'), 'Boot-Position übernehmen');
  const refNote = note();
  const refErr = h('div', { class: 'note danger', role: 'alert', hidden: true });
  async function setRef(x, y) {
    refErr.hidden = true;
    try {
      await api.reference(x, y);
      toast(`Referenz gesetzt: X ${fmt(x, 1)} · Y ${fmt(y, 1)} mm.`, 'ok');
    } catch (e) {
      if (e instanceof ApiError) { refErr.textContent = e.message; refErr.hidden = false; }
      throw e;
    }
  }
  refBtn.addEventListener('click', () => run(refBtn, async () => {
    const x = rx.get(), y = ry.get();
    if (x == null || y == null) { toast('Bitte X und Y der Gondel eintragen.', 'error'); return; }
    await setRef(x, y);
  }));
  bootBtn.addEventListener('click', () => run(bootBtn, async () => {
    const b = store.get('state')?.reference?.boot_physical ?? [0, 0];
    rx.input.value = String(b[0]); ry.input.value = String(b[1]);
    await setRef(b[0], b[1]);
  }));
  const refSect = sect('Referenz',
    refState,
    h('p', { class: 'muted', style: { fontSize: '14px' } }, 'Nach dem Einschalten oder Reset glaubt die Firmware, die Gondel hänge in ihrem Ursprung. Die Referenz muss dazu passen, wo die Gondel wirklich hängt.'),
    bootBtn,
    h('div', { class: 'grid2' }, rx.el, ry.el), presets, refBtn, refErr, refNote);

  /* ---------- Jog ---------- */
  const stepSeg = segmented({ label: 'Schrittweite', value: store.get('step'), fill: true,
    options: STEPS.map((s) => ({ value: s, label: `${s} mm` })),
    onChange: (v) => store.set({ step: v }) });
  const feedF = numField({ label: 'Geschwindigkeit', unit: 'mm/min', step: 100, min: 60, onChange: (v) => { store.set({ feed: v }); lsSet('wbot-feed', v); } });
  const feedSeg = segmented({ label: 'Geschwindigkeits-Vorgaben', value: null, fill: true,
    options: [{ value: 600, label: 'Langsam' }, { value: 1500, label: 'Mittel' }, { value: 3000, label: 'Schnell' }],
    onChange: (v) => { store.set({ feed: v }); feedF.set(v); } });
  let jogBusy = false;
  async function jog(dx, dy) {
    if (jogBusy) return;
    const c = caps(store.get('state'), store.get('job'));
    if (c.move) { toast(c.move, 'error'); return; }
    jogBusy = true;
    try { await api.jog(dx * store.get('step'), dy * store.get('step'), store.get('feed')); }
    catch { /* Toast kam aus api.js */ }
    finally { jogBusy = false; }
  }
  const mk = (ic, label, dx, dy) => h('button', { class: 'btn', type: 'button', 'aria-label': label, dataset: { title: label }, onClick: () => jog(dx, dy) }, icon(ic));
  const bUp = mk('up', 'Nach oben (Pfeil hoch)', 0, 1), bDn = mk('down', 'Nach unten (Pfeil runter)', 0, -1);
  const bL = mk('left', 'Nach links (Pfeil links)', -1, 0), bR = mk('right', 'Nach rechts (Pfeil rechts)', 1, 0);
  const mid = h('div', { class: 'mid' });
  const jogNote = note();
  const penWarn = h('div', { class: 'note warn' }, 'Der Stift ist unten: Jog zeichnet jetzt eine Linie. Erst „Stift oben“ wählen, wenn das nicht gewollt ist.');
  const penUnknown = h('div', { class: 'note' }, 'Stift-Position unbekannt – wird vor der ersten Fahrt gehoben.');
  const refWarn = h('div', { class: 'note' }, 'Ohne Referenz fährt der Jog in Firmware-Koordinaten (ungenau, nur zum Grobpositionieren).');
  const jogSect = sect('Schritt-Fahrt',
    stepSeg.el,
    h('div', { class: 'pad' }, h('span'), bUp, h('span'), bL, mid, bR, h('span'), bDn, h('span')),
    h('div', { class: 'row' }, feedF.el), feedSeg.el, penWarn, penUnknown, refWarn, jogNote,
    h('p', { class: 'field-hint' }, 'Tastatur: Pfeiltasten fahren einen Schritt. Nicht im Eingabefeld.'));

  /* ---------- Gehe zu ---------- */
  const gx = numField({ label: 'Ziel X', unit: 'mm', step: 1, value: 750 });
  const gy = numField({ label: 'Ziel Y', unit: 'mm', step: 1, value: 500 });
  const goBtn = h('button', { class: 'btn primary', type: 'button' }, icon('target'), 'Fahren');
  const parkBtn = h('button', { class: 'btn', type: 'button' }, icon('home'), 'Park');
  const goNote = note();
  const rangeHint = h('p', { class: 'field-hint' });
  goBtn.addEventListener('click', () => run(goBtn, async () => {
    const x = gx.get(), y = gy.get();
    if (x == null || y == null) { toast('Bitte X und Y des Ziels eintragen.', 'error'); return; }
    await api.goto(x, y, store.get('feed'));
    toast(`Fahre nach X ${fmt(x, 1)} · Y ${fmt(y, 1)} mm.`, 'ok');
  }));
  parkBtn.addEventListener('click', () => run(parkBtn, async () => { await api.park(); toast('Fahre zur Park-Position.', 'ok'); }));
  const goSect = sect('Gehe zu', h('div', { class: 'grid2' }, gx.el, gy.el), rangeHint,
    h('div', { class: 'row' }, goBtn, parkBtn), goNote);

  /* ---------- Stift & Farbe ---------- */
  const upBtn = h('button', { class: 'btn', type: 'button' }, icon('up'), 'Stift oben');
  const downBtn = h('button', { class: 'btn', type: 'button' }, icon('down'), 'Stift unten');
  upBtn.addEventListener('click', () => run(upBtn, () => api.pen('up')));
  downBtn.addEventListener('click', () => run(downBtn, () => api.pen('down')));
  const colorWrap = h('div', { class: 'grid3' });
  const penNote = note();
  const penSect = sect('Stift und Farbe', h('div', { class: 'grid2' }, upBtn, downBtn), colorWrap,
    h('p', { class: 'field-hint' }, 'Beim Farbwechsel wird der Stift vor dem Drehen des Revolvers gehoben.'), penNote);

  root.append(refSect, jogSect, goSect, penSect);

  /* ---------- Aktualisieren ---------- */
  let colorSig = '', inited = false;
  function update() {
    const s = store.get('state'), cfg = store.get('config'), c = caps(s, store.get('job'));
    if (cfg && !inited) {
      inited = true;
      const p = cfg.motion.park_position, f = store.get('feed') || cfg.motion.jog_feed_mm_min;
      feedF.set(f);
      gx.set(p[0]); gy.set(p[1]);
      const mid = [cfg.board.width_mm / 2, cfg.board.height_mm / 2];
      presets.replaceChildren(h('span', { class: 'faint', style: { fontSize: '12.5px' } }, 'Schnellwahl:'),
        ...[['Park', p], ['Brettmitte', mid], ['Ursprung', [0, 0]]].map(([n, v]) => h('button', { class: 'btn sm', type: 'button',
          onClick: () => { rx.input.value = String(v[0]); ry.input.value = String(v[1]); } }, `${n} (${fmt(v[0], 0)} | ${fmt(v[1], 0)})`)));
      rx.set(p[0]); ry.set(p[1]);
    }
    if (cfg) {
      const r = s?.limits?.safe_rect ?? [cfg.board.margin_mm, cfg.board.margin_mm, cfg.board.width_mm - cfg.board.margin_mm, cfg.board.height_mm - cfg.board.margin_mm];
      rangeHint.textContent = `Erlaubter Bereich: X ${fmt(r[0], 0)}…${fmt(r[2], 0)}, Y ${fmt(r[1], 0)}…${fmt(r[3], 0)} mm.`;
    }
    const ref = s?.reference;
    refState.className = 'note ' + (ref?.set ? 'ok' : 'warn');
    refState.textContent = !c.connected ? 'Nicht verbunden.'
      : ref?.set ? `Referenz gesetzt: Gondel bei X ${fmt(ref.physical?.[0], 1)} · Y ${fmt(ref.physical?.[1], 1)} mm (Firmware ${fmt(ref.firmware?.[0], 2)} | ${fmt(ref.firmware?.[1], 2)}).`
        : 'Keine Referenz. Absolute Fahrten, Zeichnen und Park sind gesperrt.';
    const lbl = ref?.set ? 'Referenz neu setzen' : 'Gondel steht jetzt hier';
    if (refBtn._lbl !== lbl) { refBtn._lbl = lbl; refBtn.replaceChildren(icon('target'), lbl); }
    const bp = ref?.boot_physical ?? [0, 0], bl = `Boot-Position übernehmen (${fmt(bp[0], 0)} | ${fmt(bp[1], 0)})`;
    if (bootBtn._lbl !== bl) { bootBtn._lbl = bl; bootBtn.replaceChildren(icon('home'), bl); }
    bootBtn.classList.toggle('primary', !ref?.set);
    gate(refBtn, c.move); gate(bootBtn, c.move); refNote.textContent = c.move;

    stepSeg.set(store.get('step'));
    [bUp, bDn, bL, bR].forEach((b) => gate(b, c.move));
    jogNote.textContent = c.move;
    mid.textContent = `${store.get('step')} mm`;
    penWarn.hidden = !(c.connected && s.pen && s.pen.known !== false && !s.pen.up);
    penUnknown.hidden = !(c.connected && s.pen && s.pen.known === false);
    refWarn.hidden = !(c.connected && !c.referenced);

    gate(goBtn, c.abs); gate(parkBtn, c.abs); goNote.textContent = c.abs;

    gate(upBtn, c.servo); gate(downBtn, c.servo);
    upBtn.setAttribute('aria-pressed', String(!!(c.connected && s.pen?.up)));
    downBtn.setAttribute('aria-pressed', String(!!(c.connected && s.pen && !s.pen.up)));
    const colors = cfg?.colors ?? [];
    const sig = JSON.stringify(colors);
    if (sig !== colorSig) {
      colorSig = sig;
      colorWrap.replaceChildren(...colors.map((col, i) => {
        const b = h('button', { class: 'btn color-btn', type: 'button', dataset: { slot: i + 1 } }, swatch(col.hex), h('span', { class: 'nm' }, `${i + 1} · ${col.name}`));
        b.addEventListener('click', () => run(b, () => api.revolver({ slot: i + 1 })));
        return b;
      }));
    }
    [...colorWrap.children].forEach((b) => {
      gate(b, c.servo);
      b.setAttribute('aria-pressed', String(c.connected && s.revolver?.slot === Number(b.dataset.slot)));
    });
    penNote.textContent = c.servo;
  }
  store.on(['state', 'config', 'job', 'step'], update, { immediate: true });

  /* ---------- Tastatur ---------- */
  window.addEventListener('keydown', (e) => {
    if (store.get('tab') !== 'fahren' || e.repeat || e.altKey || e.ctrlKey || e.metaKey || isTyping(e.target)) return;
    if (e.target.closest?.('dialog')) return;
    const m = { ArrowUp: [0, 1], ArrowDown: [0, -1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] }[e.key];
    if (!m) return;
    e.preventDefault();
    jog(m[0], m[1]);
  });
}
