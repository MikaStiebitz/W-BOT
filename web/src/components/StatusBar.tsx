import { api } from "../api";
import { useStore } from "../store";

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "–";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m) return `${m} min ${String(s).padStart(2, "0")} s`;
  return `${s} s`;
}

const STATE_LABEL: Record<string, string> = {
  disconnected: "Getrennt",
  idle: "Bereit",
  running: "Zeichnet",
  paused: "Pausiert",
  alarm: "Alarm",
  estop: "Not-Halt",
  homing: "Referenzfahrt",
  jogging: "Fährt",
};

export function StatusBar() {
  const { status, config, connected, error, busy, run, setError, ams } = useStore();
  const activeSlot = ams[status?.color ?? 0];
  const progress = status?.progress;
  const running = status?.state === "running";
  const paused = status?.state === "paused";
  const faulted = status?.state === "alarm" || status?.state === "estop";

  return (
    <header className="statusbar">
      <div className="statusbar-main">
        <span className="brand" aria-label="wbbot">
          <span className="brand-mark" aria-hidden="true">▲</span>WBBOT
        </span>
        <span className={`pill pill-${status?.state ?? "disconnected"}`}>
          {STATE_LABEL[status?.state ?? "disconnected"] ?? status?.state}
        </span>

        <span className="stat">
          <b>Verbindung</b>
          {connected ? (config?.transport ?? "verbunden") : "getrennt"}
        </span>

        <span className="stat">
          <b>Position</b>
          {status ? `${status.position.x.toFixed(0)}, ${status.position.y.toFixed(0)} mm` : "–"}
        </span>

        <span className="stat">
          <b>Stift</b>
          {status?.pen_down ? "unten" : "oben"}
        </span>

        <span className="stat">
          <b>Marker</b>
          <span className="swatch"
                style={{ background: activeSlot?.hex
                  ?? config?.colors[status?.color ?? 0]?.hex ?? "#111" }} />
          {activeSlot?.name ?? config?.colors[status?.color ?? 0]?.name ?? "–"}
        </span>

        {progress && progress.lines_total > 0 && (
          <>
            <span className="stat progress-stat">
              <b>Fortschritt</b>
              <span className="progress-track"
                    role="progressbar"
                    aria-valuenow={Math.round(progress.percent)}
                    aria-valuemin={0} aria-valuemax={100}>
                <span className="progress-fill"
                      style={{ transform: `scaleX(${progress.percent / 100})` }} />
              </span>
              {progress.percent.toFixed(0)} %
            </span>
            <span className="stat">
              <b>Restzeit</b>
              {formatDuration(progress.eta_s)}
            </span>
          </>
        )}
      </div>

      <div className="statusbar-actions">
        {!connected && (
          <button onClick={() => run(() => api.connect())} disabled={busy}>
            Verbinden
          </button>
        )}
        {running && (
          <button onClick={() => run(() => api.pause())} disabled={busy}>Pause</button>
        )}
        {paused && (
          <button onClick={() => run(() => api.resume())} disabled={busy}>Weiter</button>
        )}
        {(running || paused) && (
          <button onClick={() => run(() => api.cancel())} disabled={busy}>Abbrechen</button>
        )}
        {status?.can_resume && !running && !paused && (
          <button onClick={() => run(() => api.resumeJob())} disabled={busy}>
            Fortsetzen (Strich {status.resume_from_stroke})
          </button>
        )}
        {faulted && (
          <button onClick={() => run(() => api.clearAlarm())} disabled={busy}>
            Alarm quittieren
          </button>
        )}
        <button className="estop" onClick={() => run(() => api.estop())}
                title="Not-Halt — auch mit der Leertaste erreichbar">
          NOT-HALT
        </button>
      </div>

      {error && (
        <div className="banner banner-error" role="alert">
          <span>{error}</span>
          <button onClick={() => setError(null)} aria-label="Meldung schliessen">×</button>
        </div>
      )}
      {status?.last_error && !error && (
        <div className="banner banner-warn">{status.last_error}</div>
      )}
    </header>
  );
}
