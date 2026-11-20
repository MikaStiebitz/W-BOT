// Tiny reactive store: set({key: value}) notifies subscribers of the changed keys.
import { lsGet, lsSet } from './util.js';

export function createStore(initial) {
  const data = { ...initial };
  const subs = new Map();   // key -> Set<fn>
  let queued = new Set();
  let scheduled = false;

  function flush() {
    scheduled = false;
    const keys = queued;
    queued = new Set();
    const fns = new Set();
    for (const k of keys) for (const f of subs.get(k) ?? []) fns.add(f);
    for (const f of fns) {
      try { f(data, keys); } catch (e) { console.error('Store subscriber failed', e); }
    }
  }

  return {
    get: (k) => data[k],
    all: () => data,
    /** Sets values; subscribers run batched in the next microtask. */
    set(patch) {
      for (const [k, v] of Object.entries(patch)) {
        if (data[k] === v && (v === null || typeof v !== 'object')) continue;
        data[k] = v;
        queued.add(k);
      }
      if (queued.size && !scheduled) { scheduled = true; queueMicrotask(flush); }
    },
    /** Like set(), but forces a notification even for the same reference (mutated arrays). */
    touch(...keys) {
      for (const k of keys) queued.add(k);
      if (queued.size && !scheduled) { scheduled = true; queueMicrotask(flush); }
    },
    on(keys, fn, { immediate = false } = {}) {
      const ks = Array.isArray(keys) ? keys : [keys];
      for (const k of ks) { if (!subs.has(k)) subs.set(k, new Set()); subs.get(k).add(fn); }
      if (immediate) { try { fn(data, new Set(ks)); } catch (e) { console.error(e); } }
      return () => { for (const k of ks) subs.get(k)?.delete(fn); };
    },
  };
}

export const store = createStore({
  state: null,          // last state from the server
  config: null,         // config as JSON
  patterns: [],         // /api/patterns
  ports: [],            // /api/ports
  job: null,            // last job
  ws: 'connecting',     // connecting | open | closed
  simInfo: null,        // /api/simulator
  preview: null,        // last preview {drawing, bounds, ...}
  trace: [],            // [[x0,y0,x1,y1,down,color], ...]   (mutated, then touch('trace'))
  logs: [],             // {n,level,text,t}                  (mutated, then touch('logs'))
  logSeq: 0,
  calib: null,          // calibration overlay {targets, measured, hot, result, rowIdx}
  camera: null,         // GET /api/camera (source, geometry, coverage)
  camTrack: null,       // WS "camera": gondola marker {t, seen, soll, pen, dev_mm}
  presence: null,       // WS "presence": person at the board {present, outline, skeleton, ...}
  anchorPreview: null,  // measured motors not yet applied {left, right, rect} (green in the live view)
  autocal: null,        // WS "autocal" / GET /api/camera/autocal
  camFrame: 0,          // counter: new camera frame for the board view
  target: null,         // clicked board point {x,y,sx,sy}
  tab: lsGet('wbot-tab', 'draw'),
  moreOpen: lsGet('wbot-more', null),   // open section of the "more" tab
  selectedPen: lsGet('wbot-pen', 0),   // pen used for single-pen images and shown in the revolver view
  step: lsGet('wbot-step', 10),
  feed: lsGet('wbot-feed', null),   // jog / travel speed mm/min (null = config)
  simOpts: lsGet('wbot-simopts', { speed: 1, start: [0, 0], perturb: {} }),
  transport: lsGet('wbot-transport', null),
  port: lsGet('wbot-port', null),
  wifi: lsGet('wbot-wifi', { host: 'fluidnc.local', protocol: 'telnet' }),   // host + protocol only, there is no password
});

/** True when panel `id` is on screen: its own tab, or its section is open inside the "more" tab. */
export function viewing(id) {
  const tab = store.get('tab');
  return tab === id || (tab === 'more' && store.get('moreOpen') === id);
}

export function persist(key, storeKey) {
  store.on(storeKey, (d) => lsSet(key, d[storeKey]));
}
