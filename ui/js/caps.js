// Was darf der Nutzer gerade? Liefert für jede Aktion einen leeren String (erlaubt) oder die deutsche Begründung.

const ACTIVE_JOB = new Set(['planning', 'running', 'paused']);

export const MACHINE_LABEL = {
  disconnected: 'Getrennt', idle: 'Bereit', run: 'Läuft', hold: 'Pause', jog: 'Jog',
  alarm: 'Alarm', door: 'Tür offen', check: 'Check',
};

export function alarmText(a) {
  if (!a) return '';
  if (typeof a === 'string') return a;
  if (typeof a === 'number') return `Alarm ${a}`;
  const code = a.code != null ? `ALARM ${a.code}: ` : '';
  return code + (a.text || a.message || 'Alarm aktiv');
}

export function caps(state, job) {
  const st = state;
  const connected = st?.connection?.status === 'connected';
  const ms = st?.machine?.state ?? 'disconnected';
  const alarm = ms === 'alarm';
  const jobActive = !!job && ACTIVE_JOB.has(job.state);
  const busy = !!st?.machine?.busy || jobActive || ms === 'run' || ms === 'jog' || ms === 'hold';
  const referenced = !!st?.reference?.set;

  const base = !connected ? 'Nicht verbunden. Oben verbinden.'
    : alarm ? 'Alarm aktiv. Erst entsperren ($X) oder zurücksetzen.' : '';
  const idle = base || (busy ? (jobActive ? 'Ein Job läuft. Erst pausieren oder abbrechen.' : 'Die Maschine fährt gerade. Warten oder Not-Halt.') : '');
  const abs = idle || (!referenced ? 'Keine Referenz. Erst „Gondel steht jetzt hier“ setzen (Reiter Fahren).' : '');

  return {
    connected, alarm, busy, jobActive, referenced, ms,
    /** Jog / Servo-Test / Stift / Revolver / Referenz setzen */
    move: idle,
    /** Servo-Befehle brauchen keine Referenz, aber freie Maschine */
    servo: idle,
    /** Absolute Fahrten: goto, park, Zeichnen */
    abs,
    /** Nur verbunden und kein Alarm (z. B. Konsole senden) */
    online: base,
    penDown: st?.pen ? !st.pen.up : false,
  };
}
