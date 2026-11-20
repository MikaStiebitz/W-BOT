import { useEffect } from "react";
import { api, ws } from "../../api";
import { useStore } from "../../store";

const STATE_LABEL: Record<string, string> = {
  stopped: "gestoppt",
  ready: "wartet auf Striche",
  drawing: "zeichnet nach",
  error: "Fehler",
};

/**
 * Asynchrones Live-Malen: auf dem Board vormalen, so schnell man will.
 *
 * Jeder fertige Strich landet mit seiner Farbe in der Warteschlange; der
 * Plotter arbeitet sie in Ruhe ab und wechselt zwischen den Strichen selbst
 * den Marker. Blass = wartet, markiert = wird gerade gezeichnet,
 * kräftig = ist bereits Tinte.
 */
export function LivePanel() {
  const {
    ams, sketchActive, sketchColor, sketchStats, sketchStrokes,
    setSketchColor, clearSketchView, run, busy, status,
  } = useStore();

  // Keys 1/2/3 pick the marker for the *next* stroke; space is the e-stop.
  useEffect(() => {
    if (!sketchActive) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.repeat) return;
      const index = ["Digit1", "Digit2", "Digit3"].indexOf(event.code);
      if (index >= 0 && index < ams.length && ams[index]?.enabled) {
        event.preventDefault();
        setSketchColor(index);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sketchActive, ams, setSketchColor]);

  const strokes = Object.values(sketchStrokes);
  const recent = strokes.slice(-8).reverse();

  const start = () => run(async () => {
    clearSketchView();
    return api.sketchStart();
  });

  const stop = () => run(() => api.sketchStop());

  const clearQueue = () => {
    ws.send("sketch_clear");
  };

  return (
    <div className="panel">
      <h2>Live malen</h2>

      <div className="actions">
        {!sketchActive ? (
          <button className="primary" onClick={start}
                  disabled={busy || status?.state !== "idle"}>
            Session starten
          </button>
        ) : (
          <>
            <button onClick={stop} disabled={busy}>Session beenden</button>
            <button onClick={clearQueue}
                    disabled={sketchStats.queued === 0}
                    title="Wirft alles Wartende weg; der aktuelle Strich wird fertig gezeichnet">
              Warteschlange leeren ({sketchStats.queued})
            </button>
          </>
        )}
      </div>

      {sketchActive && (
        <>
          <p className="hint">
            Male auf dem Board vor — so schnell du willst. Jeder Strich landet
            in der Warteschlange und der Plotter zeichnet asynchron nach.
          </p>

          <div className="field">
            <label>Farbe für neue Striche (Tasten 1 / 2 / 3)</label>
            <div className="color-row">
              {ams.map((slot) => (
                <button
                  key={slot.index}
                  className={`color-chip${sketchColor === slot.index ? " active" : ""}`}
                  style={{ background: slot.enabled ? slot.hex : "#e5e7eb" }}
                  onClick={() => slot.enabled && setSketchColor(slot.index)}
                  disabled={!slot.enabled}
                  title={slot.enabled ? slot.name : `Slot ${slot.index + 1} ist leer`}
                >
                  {slot.index + 1}
                </button>
              ))}
            </div>
          </div>

          <div className="report">
            <dl>
              <dt>Zustand</dt>
              <dd>{STATE_LABEL[sketchStats.state] ?? sketchStats.state}</dd>
              <dt>Wartend</dt><dd>{sketchStats.queued}</dd>
              <dt>Gezeichnet</dt><dd>{sketchStats.done}</dd>
              {sketchStats.failed > 0 && (
                <><dt>Fehlgeschlagen</dt>
                  <dd className="bad">{sketchStats.failed}</dd></>
              )}
            </dl>
          </div>

          {recent.length > 0 && (
            <ul className="queue-list" aria-label="Letzte Striche">
              {recent.map((stroke) => (
                <li key={stroke.id} className={`queue-item ${stroke.status}`}>
                  <span className="queue-dot"
                        style={{ background: ams[stroke.color]?.hex ?? "#111" }} />
                  Strich {stroke.id}
                  <span className="queue-status">
                    {stroke.status === "queued" && "wartet"}
                    {stroke.status === "drawing" && "zeichnet…"}
                    {stroke.status === "done" && "fertig"}
                    {stroke.status === "failed" && "fehlgeschlagen"}
                  </span>
                </li>
              ))}
            </ul>
          )}

          <div className="banner banner-info">
            <b>Kein Undo.</b> Wartende Striche lassen sich verwerfen —
            was gezeichnet ist, steht auf dem Board.
          </div>

          <p className="hint">
            <b>Leertaste</b> löst jederzeit den Not-Halt aus und leert die
            Warteschlange.
          </p>
        </>
      )}

      {!sketchActive && (
        <p className="muted">
          Im Live-Modus malst du auf dem Board vor und der Plotter zeichnet
          asynchron nach — auch mit verschiedenen Farben pro Strich. Die
          Maschine muss dafür im Zustand „Bereit“ sein.
        </p>
      )}
    </div>
  );
}
