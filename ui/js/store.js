// Winziger reaktiver Store: set({key: wert}) benachrichtigt Abonnenten der geänderten Schlüssel.
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
      try { f(data, keys); } catch (e) { console.error('Store-Abonnent fehlgeschlagen', e); }
    }
  }

  return {
    get: (k) => data[k],
    all: () => data,
    /** Setzt Werte; Abonnenten laufen gebündelt im nächsten Microtask. */
    set(patch) {
      for (const [k, v] of Object.entries(patch)) {
        if (data[k] === v && (v === null || typeof v !== 'object')) continue;
        data[k] = v;
        queued.add(k);
      }
      if (queued.size && !scheduled) { scheduled = true; queueMicrotask(flush); }
    },
    /** Wie set(), erzwingt Benachrichtigung auch bei gleicher Referenz (mutierte Arrays). */
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
  state: null,          // letzter State vom Server
  config: null,         // Config als JSON
  patterns: [],         // /api/patterns
  ports: [],            // /api/ports
  job: null,            // letzter Job
  ws: 'connecting',     // connecting | open | closed
  simInfo: null,        // /api/simulator
  preview: null,        // letzte Vorschau {drawing, bounds, ...}
  trace: [],            // [[x0,y0,x1,y1,down,color], ...]   (mutiert, dann touch('trace'))
  logs: [],             // {n,level,text,t}                  (mutiert, dann touch('logs'))
  logSeq: 0,
  calib: null,          // Kalibrier-Overlay {targets, measured, hot, result, rowIdx}
  camera: null,         // GET /api/camera (Quelle, Geometrie, Abdeckung)
  camTrack: null,       // WS "camera": Gondel-Marker {t, seen, soll, pen, dev_mm}
  autocal: null,        // WS "autocal" / GET /api/camera/autocal
  camFrame: 0,          // Zähler: neues Kamerabild für die Brettansicht
  target: null,         // angeklickter Brettpunkt {x,y,sx,sy}
  tab: lsGet('wbot-tab', 'fahren'),
  step: lsGet('wbot-step', 10),
  feed: lsGet('wbot-feed', null),   // Jog-/Fahrgeschwindigkeit mm/min (null = Config)
  simOpts: lsGet('wbot-simopts', { speed: 1, start: [0, 0], perturb: {} }),
  transport: lsGet('wbot-transport', null),
  port: lsGet('wbot-port', null),
  wifi: lsGet('wbot-wifi', { host: 'fluidnc.local', protocol: 'telnet' }),   // nur Host + Protokoll, es gibt kein Passwort
});

export function persist(key, storeKey) {
  store.on(storeKey, (d) => lsSet(key, d[storeKey]));
}
