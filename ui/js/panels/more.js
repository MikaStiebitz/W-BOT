// "More" tab: everything advanced, one collapsed section at a time, mounted only when first opened.
import { h } from '../util.js';
import { store } from '../store.js';
import { lsSet } from '../util.js';
import { mountCamera } from './camera.js';
import { mountCalibration } from './calibration.js';
import { mountServo } from './servo.js';
import { mountSimulator } from './simulator.js';
import { mountConsole } from './console.js';
import { mountSettings } from './settings.js';

const SECTIONS = [
  { id: 'kamera', label: 'Camera', hint: 'Markers, lens, people guard, camera calibration', mount: mountCamera },
  { id: 'kalib', label: 'Calibration', hint: 'Fine-tune the geometry with measured crosses', mount: mountCalibration },
  { id: 'servo', label: 'Servos', hint: 'Test and tune the pen and revolver servos', mount: mountServo },
  { id: 'sim', label: 'Simulator', hint: 'Inject errors to test the calibration', mount: mountSimulator,
    when: (s) => s?.connection?.transport === 'sim' && s.connection.status === 'connected' },
  { id: 'konsole', label: 'Console', hint: 'G-code and the machine log', mount: mountConsole },
  { id: 'einst', label: 'Settings', hint: 'Board size, motion, geometry, firmware config', mount: mountSettings },
];

export function mountMore(root) {
  const items = {};
  for (const s of SECTIONS) {
    const body = h('div', { class: 'more-panel' });
    const det = h('details', { class: 'acc', dataset: { id: s.id } },
      h('summary', null, h('span', { class: 'acc-t' }, s.label), h('span', { class: 'acc-h faint' }, s.hint)), body);
    let mounted = false;
    const ensure = () => {
      if (mounted) return; mounted = true;
      try { s.mount(body); } catch (e) { console.error(e); body.append(h('div', { class: 'note danger' }, `“${s.label}” could not be loaded: ${e.message}`)); }
    };
    det.addEventListener('toggle', () => {
      if (det.open) {
        ensure();
        for (const [id, o] of Object.entries(items)) if (id !== s.id && o.det.open) o.det.open = false;
        store.set({ moreOpen: s.id }); lsSet('wbot-more', s.id);
      } else if (store.get('moreOpen') === s.id) { store.set({ moreOpen: null }); lsSet('wbot-more', null); }
    });
    items[s.id] = { det, ensure };
    root.append(det);
  }
  const open = store.get('moreOpen');
  if (open && items[open]) { items[open].det.open = true; items[open].ensure(); }

  store.on('state', (d) => { for (const s of SECTIONS) if (s.when) items[s.id].det.hidden = !s.when(d.state); }, { immediate: true });
  /** Opens a section from elsewhere (e.g. the Setup tab). */
  window.addEventListener('wbot:open-more', (e) => { const it = items[e.detail]; if (it) { store.set({ tab: 'more' }); it.det.open = true; } });
}
