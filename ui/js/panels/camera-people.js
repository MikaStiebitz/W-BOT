// Camera tab: people at the board (skeleton, guard, queue, hide) and following camera movement.
import { h, fmt } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, toast } from '../ui.js';
import { segmented, toggle } from '../widgets.js';

const WATCH = { ok: 'Camera is steady, alignment fits.', followed: 'Camera was moved, alignment followed automatically', lost: 'Camera moved a lot: please align the board again.', check: 'Checking whether the camera moved since the last start ...', none: 'No reference picture yet (taken when you align the board).' };

export function mountPeople(root, sect, p) {
  const ppl = () => store.get('camera')?.settings?.people ?? {};
  const put = async (patch) => store.set({ camera: await api.cameraPut(patch) });
  const guard = segmented({ label: 'Guard', fill: true, value: ppl().guard ?? 'warn', options: [
    { value: 'off', label: 'Off' }, { value: 'warn', label: 'Show and warn' }, { value: 'protect', label: 'Protect', title: 'No start and pause while something is in front of the board' }],
  onChange: (v) => put({ people: { guard: v } }) });
  const hide = toggle({ label: 'Hide objects in the picture', onChange: (v) => put({ people: { filter: v } }) });
  const resume = toggle({ label: 'Resume a paused job once the board is free', onChange: (v) => put({ people: { resume: v } }) });
  const follow = toggle({ label: 'Follow camera movement automatically', onChange: (v) => put({ watch: { enabled: v } }) });
  const status = h('div', { class: 'stats' });
  const unqueue = h('button', { class: 'btn sm', type: 'button' }, icon('x'), 'Drop waiting job');
  const refresh = async () => store.set({ camera: await api.camera({ quiet: true }) });
  unqueue.addEventListener('click', () => run(unqueue, async () => { await fetch('/api/camera/queue-job', { method: 'DELETE' }); await refresh(); toast('Waiting job dropped.', 'info'); }));
  const queueBox = h('div', { class: 'row', hidden: true }, h('span', { class: 'chip' }, 'A job waits for the board to be free.'), unqueue);
  const watchLine = h('div', { class: 'muted', role: 'status', style: { fontSize: '13px' } });

  function paint() {
    const cam = store.get('camera'), s = ppl(), pr = store.get('presence') ?? cam?.presence ?? {};
    guard.set(s.guard ?? 'warn'); hide.set(!!s.filter); resume.set(s.resume !== false); follow.set(cam?.settings?.watch?.enabled !== false);
    const fresh = pr.t && Date.now() / 1000 - pr.t < 3;
    const cell = (v, l) => h('div', { class: 'stat' }, h('b', null, v), h('span', null, l));
    status.replaceChildren(cell(!cam?.source ? '–' : fresh && pr.present ? 'yes' : 'no', 'Something in front'),
      cell(fresh && pr.present ? `${fmt(pr.since ?? 0, 0)} s` : '–', 'for'),
      cell(pr.paused_by_person ? 'paused' : s.guard === 'protect' ? 'on' : s.guard === 'off' ? 'off' : 'warns', 'Guard'));
    queueBox.hidden = !(pr.queued ?? cam?.queued);
    const w = cam?.watch ?? {};
    watchLine.textContent = (WATCH[w.state] ?? '') + (w.state === 'followed' && w.shift_px != null ? ` (${fmt(w.shift_px, 0)} px).` : '');
  }

  root.append(sect('8 Objects at the board',
    p('Anything in front of the board (a person, a ladder ...) is outlined live; the largest gets a skeleton. "Protect": no job starts while the board is blocked, a running job pauses, and a job can wait to start once it is free.'),
    guard.el, status, queueBox, hide.el, resume.el),
  sect('9 Camera moved?',
    p('Aligning the board stores a reference picture. If the camera moves (also between two starts), the alignment follows it.'),
    follow.el, watchLine));
  store.on(['camera', 'presence'], paint, { immediate: true });
}
