// Simulator connection options (error injection), shared by the header and the simulator panel.
import { store } from './store.js';

export const NEUTRAL = { anchor_left_dx: 0, anchor_left_dy: 0, anchor_right_dx: 0, anchor_right_dy: 0, radius: null, scale_left: 1, scale_right: 1, pen_dx: 0, pen_dy: 0 };

/** Send only non-neutral error values; empty = error-free simulator. */
export function perturbBody(p) {
  const out = {};
  for (const [k, neutral] of Object.entries(NEUTRAL)) {
    const v = p?.[k];
    if (v == null || Number.isNaN(v)) continue;
    if (neutral === null ? v > 0 : Math.abs(v - neutral) > 1e-9) out[k] = v;
  }
  return out;
}

export function connectBody() {
  const t = store.get('transport') || store.get('config')?.connection?.transport || 'sim';
  if (t === 'sim') {
    const o = store.get('simOpts') || {};
    const sim = { speed: o.speed || 1, perturb: perturbBody(o.perturb) };
    if (o.start && o.start.length === 2) sim.physical_start = o.start;
    return { transport: 'sim', sim };
  }
  if (t === 'wifi') {
    const w = store.get('wifi') || {};
    const m = /^([^:]+):(\d{1,5})$/.exec((w.host || '').trim());          // "host:port" allowed (e.g. fake board 127.0.0.1:2323)
    const body = { transport: 'wifi', host: m ? m[1] : (w.host || '').trim(), protocol: w.protocol === 'websocket' ? 'websocket' : 'telnet' };
    if (m) body.port = Number(m[2]);
    return body;
  }
  const cfg = store.get('config')?.connection?.serial;
  return { transport: 'serial', port: store.get('port') || cfg?.port || null, baudrate: cfg?.baudrate || 115200 };
}
