import { useState } from "react";
import { api } from "../../api";
import { useStore } from "../../store";

export function ManualPanel() {
  const { config, status, run, busy } = useStore();
  const [target, setTarget] = useState({ x: 750, y: 500 });
  const [step, setStep] = useState(50);

  const jog = (dx: number, dy: number) => {
    const x = (status?.position.x ?? 0) + dx * step;
    const y = (status?.position.y ?? 0) + dy * step;
    setTarget({ x: Math.round(x), y: Math.round(y) });
    void run(() => api.move(x, y));
  };

  return (
    <div className="panel">
      <h2>Manuell</h2>
      <p className="hint">
        Klick auf das Board fährt die Gondel dorthin. Ziele ausserhalb des
        sicheren Bereichs werden abgelehnt, nicht abgeschnitten.
      </p>

      <div className="field">
        <label htmlFor="jog-step">Schrittweite: {step} mm</label>
        <input id="jog-step" type="range" min={1} max={200} step={1}
               value={step} onChange={(e) => setStep(+e.target.value)} />
      </div>

      <div className="jogpad">
        <button onClick={() => jog(0, 1)} disabled={busy} aria-label="nach oben">↑</button>
        <div className="jogpad-row">
          <button onClick={() => jog(-1, 0)} disabled={busy} aria-label="nach links">←</button>
          <button onClick={() => run(() => api.move(...(config?.motion.park_position ?? [750, 950])))}
                  disabled={busy} title="Zur Parkposition">⌂</button>
          <button onClick={() => jog(1, 0)} disabled={busy} aria-label="nach rechts">→</button>
        </div>
        <button onClick={() => jog(0, -1)} disabled={busy} aria-label="nach unten">↓</button>
      </div>

      <div className="grid2">
        <label>X (mm)
          <input type="number" value={target.x}
                 onChange={(e) => setTarget({ ...target, x: +e.target.value })} />
        </label>
        <label>Y (mm)
          <input type="number" value={target.y}
                 onChange={(e) => setTarget({ ...target, y: +e.target.value })} />
        </label>
      </div>
      <div className="actions">
        <button onClick={() => run(() => api.move(target.x, target.y))} disabled={busy}>
          Anfahren
        </button>
      </div>

      <div className="field">
        <label>Stift</label>
        <div className="actions">
          <button onClick={() => run(() => api.pen(true))} disabled={busy}>Senken</button>
          <button onClick={() => run(() => api.pen(false))} disabled={busy}>Heben</button>
        </div>
      </div>

      <div className="field">
        <label>Farbe</label>
        <div className="color-row">
          {config?.colors.map((color) => (
            <button key={color.index}
                    className={`color-chip${status?.color === color.index ? " active" : ""}`}
                    style={{ background: color.hex }}
                    onClick={() => run(() => api.color(color.index))}
                    disabled={busy}
                    title={color.name}>
              {color.index + 1}
            </button>
          ))}
        </div>
      </div>

      <hr />
      <div className="actions">
        <button onClick={() => run(() => api.home())} disabled={busy}>
          Referenzfahrt
        </button>
        <button onClick={() => run(() => api.disconnect())} disabled={busy}>
          Trennen
        </button>
      </div>
    </div>
  );
}
