// What may the user do right now? For each action: an empty string (allowed) or the reason it is blocked.

const ACTIVE_JOB = new Set(['planning', 'running', 'paused']);

export const MACHINE_LABEL = {
  disconnected: 'Disconnected', idle: 'Ready', run: 'Running', hold: 'Paused', jog: 'Jogging',
  alarm: 'Alarm', door: 'Door open', check: 'Check',
};

export function alarmText(a) {
  if (!a) return '';
  if (typeof a === 'string') return a;
  if (typeof a === 'number') return `Alarm ${a}`;
  const code = a.code != null ? `ALARM ${a.code}: ` : '';
  return code + (a.text || a.message || 'Alarm active');
}

export function caps(state, job) {
  const st = state;
  const connected = st?.connection?.status === 'connected';
  const ms = st?.machine?.state ?? 'disconnected';
  const alarm = ms === 'alarm';
  const jobActive = !!job && ACTIVE_JOB.has(job.state);
  const busy = !!st?.machine?.busy || jobActive || ms === 'run' || ms === 'jog' || ms === 'hold';
  const referenced = !!st?.reference?.set;

  const base = !connected ? 'Not connected. Connect at the top.'
    : alarm ? 'Alarm active. Unlock ($X) or reset first.' : '';
  const idle = base || (busy ? (jobActive ? 'A job is running. Pause or cancel it first.' : 'The machine is moving. Wait or press E-stop.') : '');
  const abs = idle || (!referenced ? 'No position reference. Set the gondola position first (Setup tab).' : '');

  return {
    connected, alarm, busy, jobActive, referenced, ms,
    /** Jog / servo test / pen / revolver / set position reference */
    move: idle,
    /** Servo commands need no position reference, but an idle machine */
    servo: idle,
    /** Absolute moves: goto, park, drawing */
    abs,
    /** Connected and no alarm only (e.g. sending from the console) */
    online: base,
    penDown: st?.pen ? !st.pen.up : false,
  };
}
