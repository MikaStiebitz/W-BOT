// Camera tab: find or set the motors (anchors) in the picture, measure where they really are and apply it.
import { h, fmt } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, toast, confirmDialog, gate } from '../ui.js';
import { toImage } from '../camera.js';

const LABELS = ['Left motor: pulley centre', 'Right motor: pulley centre'];
const xy = (v) => `${fmt(v[0], 1)} | ${fmt(v[1], 1)}`;

export function mountAnchors(root, sect, p, view) {
  let result = null, editing = false;
  const findBtn = h('button', { class: 'btn primary', type: 'button' }, icon('target'), 'Detect motors');
  const handBtn = h('button', { class: 'btn', type: 'button' }, 'Set by hand');
  const measBtn = h('button', { class: 'btn primary', type: 'button', hidden: true }, 'Measure');
  const cancelBtn = h('button', { class: 'btn', type: 'button', hidden: true }, 'Cancel');
  const applyBtn = h('button', { class: 'btn', type: 'button', hidden: true }, icon('save'), 'Apply to configuration');
  const hint = h('div', { class: 'note', hidden: true }, 'Drag the two points onto the pulley centres (a magnifier appears while dragging), then measure.');
  const out = h('div', { class: 'stack' });

  function startEdit(points) {
    editing = true; result = null;
    view.edit(points, { labels: LABELS, max: 2 });
    paint();
  }
  function stopEdit() { editing = false; view.edit(null); paint(); }
  const preview = () => store.set({ anchorPreview: result && !result.applied ? { left: result.anchor_left, right: result.anchor_right, rect: result.area?.rect ?? null } : null });

  findBtn.addEventListener('click', () => run(findBtn, async () => {
    try { const r = await api.cameraMotors(); startEdit(r.image_points); }
    catch { startEdit(configPoints()); }       // the message came as a toast; continue by hand
  }));
  handBtn.addEventListener('click', () => startEdit(configPoints()));
  cancelBtn.addEventListener('click', () => { result = null; preview(); stopEdit(); });
  measBtn.addEventListener('click', () => run(measBtn, async () => {
    const pts = view.editPoints();
    if (!pts || pts.length < 2) { toast('Please set both points.', 'error'); return; }
    const [a, b] = pts[0][0] <= pts[1][0] ? pts : [pts[1], pts[0]];
    result = await api.cameraAnchors({ left: a, right: b });
    result.points = [a, b];
    preview(); paint();
  }));
  applyBtn.addEventListener('click', () => run(applyBtn, async () => {
    const r = result;
    const ok = await confirmDialog({ title: 'Apply anchors?', text: `Left anchor ${xy(r.config_left)} → ${xy(r.anchor_left)} mm, right ${xy(r.config_right)} → ${xy(r.anchor_right)} mm.${r.area ? ` The drawing area becomes x ${fmt(r.area.rect[0], 0)}–${fmt(r.area.rect[2], 0)}, y ${fmt(r.area.rect[1], 0)}–${fmt(r.area.rect[3], 0)} mm (green in the live view; adjustable in the settings).` : ''} The firmware stays unchanged.`, ok: 'Apply' });
    if (!ok) return;
    const res = await api.cameraAnchors({ left: r.points[0], right: r.points[1], apply: true });
    if (res.config) store.set({ config: res.config });
    toast('Anchors applied and saved.', 'ok');
    result = { ...res, points: r.points, applied: true };
    preview(); stopEdit();
  }));

  function configPoints() {
    const cam = store.get('camera'), g = cam?.geometry, geo = store.get('config')?.geometry;
    const z = cam?.settings?.anchor_z_mm ?? 41, [w, hh] = cam?.source?.size ?? [1920, 1080];
    const fallback = [[w * 0.3, hh * 0.15], [w * 0.7, hh * 0.15]];
    if (!g || !geo) return fallback;
    return [geo.anchor_left, geo.anchor_right].map((a, i) => {
      const q = toImage(g, a[0], a[1], z);
      return q && q[0] >= 0 && q[0] <= w && q[1] >= 0 && q[1] <= hh ? q : fallback[i];
    });
  }

  function paint() {
    const cam = store.get('camera');
    const why = !cam?.source ? 'Choose a camera first.' : !cam?.calibrated ? 'Align the board first.' : '';
    gate(findBtn, why); gate(handBtn, why);
    findBtn.hidden = handBtn.hidden = editing;
    measBtn.hidden = cancelBtn.hidden = hint.hidden = !editing;
    out.replaceChildren();
    const r = result;
    applyBtn.hidden = !r || r.applied;
    if (!r) return;
    const cell = (v, l) => h('div', { class: 'stat' }, h('b', null, v), h('span', null, l));
    out.append(h('div', { class: 'stats' },
      cell(xy(r.anchor_left), `Left anchor (config ${xy(r.config_left)})`),
      cell(xy(r.anchor_right), `Right anchor (config ${xy(r.config_right)})`),
      cell(`${fmt(r.distance_mm, 1)} mm`, `Anchor distance (config ${fmt(r.config_distance_mm, 1)})`),
      cell(`± ${fmt(r.uncertainty_mm, 1)} mm`, 'Accuracy, roughly'),
      cell(r.area ? `x ${fmt(r.area.rect[0], 0)}–${fmt(r.area.rect[2], 0)}, y ${fmt(r.area.rect[1], 0)}–${fmt(r.area.rect[3], 0)}` : '–', 'Drawing area that fits')));
    out.append(h('p', { class: 'muted', style: { fontSize: '14px' } }, `Off from the configuration: left ${fmt(r.deviation_mm[0], 1)} mm, right ${fmt(r.deviation_mm[1], 1)} mm.`));
    for (const w of r.warnings ?? []) out.append(h('div', { class: 'note warn' }, w));
    if (r.applied) out.append(h('div', { class: 'note ok' }, 'Applied. For the last millimetres run the precise calibration below.'));
  }

  root.append(sect('4 Motors',
    p('Where the pulleys really sit matters most for accuracy. Both motors must be in the picture; the result is good to a few millimetres.'),
    h('div', { class: 'grid2' }, findBtn, handBtn), hint, h('div', { class: 'grid2' }, measBtn, cancelBtn), out, applyBtn));
  store.on('camera', paint, { immediate: true });
}
