import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { useStore } from "../../store";
import type { CalibrationState, CornerState } from "../../types";

type Method = "none" | "corners" | "pattern";

const JOG_STEPS = [0.5, 1, 5, 10, 50];

export function CalibrationPanel() {
  const { calibration, setCalibration, setPlannedPaths, run, busy, config, status } = useStore();
  const [method, setMethod] = useState<Method>("none");
  const [grid, setGrid] = useState({ x: 3, y: 3 });
  const [reference, setReference] = useState({ x: 750, y: 500, left: 0, right: 0 });
  const [measured, setMeasured] = useState<Record<number, { x: string; y: string }>>({});
  const [report, setReport] = useState<string | null>(null);
  const [servo, setServo] = useState<CalibrationState["servo"] | null>(null);

  // -- corner walk state -------------------------------------------------
  const [corners, setCorners] = useState<CornerState | null>(null);
  const [cornerIndex, setCornerIndex] = useState(0);
  const [jogStep, setJogStep] = useState(10);
  // The client tracks its own target so rapid keypresses never race the
  // (lagging) status reports.
  const jogTargetRef = useRef<[number, number]>([750, 500]);

  const step = calibration?.step ?? "idle";
  const targets = calibration?.pattern?.targets ?? [];

  useEffect(() => {
    void api.calibServo({}).then(setServo).catch(() => setServo(null));
  }, []);

  // -- corner flow -------------------------------------------------------

  const beginCorners = () => run(async () => {
    const state = await api.cornersBegin();
    setCalibration(state);
    setCorners(state.corners);
    setCornerIndex(0);
    setReport(null);
    setMethod("corners");
    const start = state.corners.targets[0];
    // Approach the first corner from well inside the board.
    jogTargetRef.current = [
      Math.min(Math.max(start[0], 60), (config?.board.width_mm ?? 1500) - 60),
      Math.min(Math.max(start[1], 60), (config?.board.height_mm ?? 1000) - 60),
    ];
    await api.cornersMove(...jogTargetRef.current);
  });

  const jog = useCallback((dx: number, dy: number) => {
    const [x, y] = jogTargetRef.current;
    const next: [number, number] = [x + dx, y + dy];
    jogTargetRef.current = next;
    void api.cornersMove(next[0], next[1]).catch((err) => {
      jogTargetRef.current = [x, y];      // refused — roll back
      useStore.getState().setError((err as Error).message);
    });
  }, []);

  const recordCorner = () => run(async () => {
    const state = await api.cornersRecord(cornerIndex);
    setCalibration(state);
    setCorners(state.corners);
    if (state.corners.missing.length === 0) {
      const solved = await api.cornersSolve();
      setReport(solved.report);
    } else {
      const next = state.corners.missing[0];
      setCornerIndex(next);
      const target = state.corners.targets[next];
      jogTargetRef.current = [
        Math.min(Math.max(target[0], 60), (config?.board.width_mm ?? 1500) - 60),
        Math.min(Math.max(target[1], 60), (config?.board.height_mm ?? 1000) - 60),
      ];
      await api.cornersMove(...jogTargetRef.current);
    }
  });

  // Arrow keys jog while the corner walk is active.
  useEffect(() => {
    if (method !== "corners" || !corners) return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.tagName === "INPUT" || target?.tagName === "SELECT") return;
      const moves: Record<string, [number, number]> = {
        ArrowUp: [0, 1], ArrowDown: [0, -1],
        ArrowLeft: [-1, 0], ArrowRight: [1, 0],
      };
      const move = moves[event.key];
      if (!move) return;
      event.preventDefault();
      jog(move[0] * jogStep, move[1] * jogStep);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [method, corners, jogStep, jog]);

  // -- pattern flow (unchanged behaviour) --------------------------------

  const beginPattern = () => run(async () => {
    setMethod("pattern");
    setReport(null);
    setCalibration(await api.calibBegin());
  });

  const submitReference = () => run(async () =>
    setCalibration(await api.calibReference(reference.x, reference.y,
                                            reference.left, reference.right)));

  const buildPattern = () => run(async () => {
    const state = await api.calibPattern(grid.x, grid.y);
    setCalibration(state);
    setPlannedPaths(state.paths);
  });

  const submitMeasurements = () => run(async () => {
    const payload = targets
      .filter((t) => measured[t.index]?.x && measured[t.index]?.y)
      .map((t) => ({
        index: t.index,
        x: Number(measured[t.index].x),
        y: Number(measured[t.index].y),
      }));
    setCalibration(await api.calibMeasure(payload));
  });

  return (
    <div className="panel">
      <h2>Kalibrierung</h2>

      {method === "none" && (
        <>
          <p className="hint">
            Zwei Wege: die schnelle <b>Ecken-Kalibrierung</b> (Gondel manuell in
            jede Board-Ecke fahren) oder das präzise <b>Messmuster</b> mit
            Massband.
          </p>
          <div className="method-cards">
            <button className="method-card" onClick={beginCorners} disabled={busy}>
              <b>Ecken (schnell)</b>
              <span>Gondel nacheinander in die vier Ecken fahren.
                Findet die Ankerpositionen in ~2 Minuten.</span>
            </button>
            <button className="method-card" onClick={beginPattern} disabled={busy}>
              <b>Messmuster (präzise)</b>
              <span>Muster zeichnen, Kreuze messen. Kalibriert alle
                10 Parameter, Restfehler unter 2 mm.</span>
            </button>
          </div>
        </>
      )}

      {/* ---------------- corner walk ---------------- */}
      {method === "corners" && corners && !report && (
        <>
          <ol className="steps">
            {corners.labels.map((label, index) => (
              <li key={label}
                  className={index === cornerIndex ? "active"
                    : corners.recorded.includes(index) ? "done" : ""}>
                {label}
              </li>
            ))}
          </ol>
          <p className="hint">
            Fahre die Stiftspitze exakt in die Ecke{" "}
            <b>{corners.labels[cornerIndex]}</b> des Boards
            (Pfeiltasten oder Steuerkreuz), dann bestätigen.
          </p>

          <div className="field">
            <label htmlFor="jog-step-cal">Schrittweite</label>
            <div className="segmented" id="jog-step-cal" role="group">
              {JOG_STEPS.map((value) => (
                <button key={value}
                        className={jogStep === value ? "active" : ""}
                        onClick={() => setJogStep(value)}>
                  {value} mm
                </button>
              ))}
            </div>
          </div>

          <div className="jogpad">
            <button onClick={() => jog(0, jogStep)} aria-label="nach oben">↑</button>
            <div className="jogpad-row">
              <button onClick={() => jog(-jogStep, 0)} aria-label="nach links">←</button>
              <button className="jog-center" disabled aria-hidden="true">✛</button>
              <button onClick={() => jog(jogStep, 0)} aria-label="nach rechts">→</button>
            </div>
            <button onClick={() => jog(0, -jogStep)} aria-label="nach unten">↓</button>
          </div>

          <p className="muted">
            Maschine meldet: {status
              ? `${status.position.x.toFixed(1)}, ${status.position.y.toFixed(1)} mm`
              : "–"}
          </p>

          <div className="actions">
            <button className="primary" onClick={recordCorner} disabled={busy}>
              Ecke „{corners.labels[cornerIndex]}“ bestätigen
              ({corners.recorded.length + 1}/4)
            </button>
            <button onClick={() => { setMethod("none"); setCorners(null); }}
                    disabled={busy}>
              Abbrechen
            </button>
          </div>
        </>
      )}

      {/* ---------------- pattern flow ---------------- */}
      {method === "pattern" && (
        <>
          <ol className="steps">
            <li className={step === "reference" ? "active" : ""}>Referenz</li>
            <li className={step === "pattern" ? "active" : ""}>Muster</li>
            <li className={step === "measure" ? "active" : ""}>Messen</li>
            <li className={step === "review" || step === "saved" ? "active" : ""}>Fit</li>
          </ol>

          {step === "reference" && (
            <>
              <p className="hint">
                Fahre die Gondel an eine bekannte Position und miss beide
                Riemenlängen mit dem Massband — das trennt den Riemen-Nullpunkt
                von der Ankerposition.
              </p>
              <div className="grid2">
                <label>X (mm)<input type="number" value={reference.x}
                  onChange={(e) => setReference({ ...reference, x: +e.target.value })} /></label>
                <label>Y (mm)<input type="number" value={reference.y}
                  onChange={(e) => setReference({ ...reference, y: +e.target.value })} /></label>
                <label>Riemen links (mm)<input type="number" value={reference.left}
                  onChange={(e) => setReference({ ...reference, left: +e.target.value })} /></label>
                <label>Riemen rechts (mm)<input type="number" value={reference.right}
                  onChange={(e) => setReference({ ...reference, right: +e.target.value })} /></label>
              </div>
              <div className="actions">
                <button className="primary" onClick={submitReference} disabled={busy}>
                  Übernehmen
                </button>
                <button onClick={() => run(async () => setCalibration(await api.calibSkipReference()))}
                        disabled={busy}>
                  Überspringen
                </button>
              </div>
            </>
          )}

          {step === "pattern" && (
            <>
              <div className="grid2">
                <label>Spalten<input type="number" min={2} max={6} value={grid.x}
                  onChange={(e) => setGrid({ ...grid, x: +e.target.value })} /></label>
                <label>Zeilen<input type="number" min={2} max={6} value={grid.y}
                  onChange={(e) => setGrid({ ...grid, y: +e.target.value })} /></label>
              </div>
              <button className="primary" onClick={buildPattern} disabled={busy}>
                Muster erzeugen
              </button>
            </>
          )}

          {step === "measure" && (
            <>
              <ul className="instructions">
                {calibration?.instructions.map((line, index) => <li key={index}>{line}</li>)}
              </ul>
              <div className="actions">
                <button onClick={() => run(() => api.calibDraw())} disabled={busy}>
                  Muster zeichnen
                </button>
              </div>
              <table className="measure-table">
                <thead>
                  <tr><th>Nr.</th><th>Soll X</th><th>Soll Y</th><th>Ist X</th><th>Ist Y</th></tr>
                </thead>
                <tbody>
                  {targets.map((target) => (
                    <tr key={target.index}>
                      <td>{target.index}</td>
                      <td>{target.nominal_x.toFixed(0)}</td>
                      <td>{target.nominal_y.toFixed(0)}</td>
                      <td><input type="number" inputMode="decimal"
                                 value={measured[target.index]?.x ?? ""}
                                 onChange={(e) => setMeasured({
                                   ...measured,
                                   [target.index]: { ...(measured[target.index] ?? { x: "", y: "" }),
                                                     x: e.target.value },
                                 })} /></td>
                      <td><input type="number" inputMode="decimal"
                                 value={measured[target.index]?.y ?? ""}
                                 onChange={(e) => setMeasured({
                                   ...measured,
                                   [target.index]: { ...(measured[target.index] ?? { x: "", y: "" }),
                                                     y: e.target.value },
                                 })} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="actions">
                <button onClick={submitMeasurements} disabled={busy}>Messwerte speichern</button>
                <button className="primary"
                        onClick={() => run(async () => {
                          const solved = await api.calibSolve();
                          setReport(solved.report);
                        })}
                        disabled={busy || (calibration?.measurements.length ?? 0) < 4}>
                  Auswerten
                </button>
              </div>
              {calibration && calibration.missing_targets.length > 0 && (
                <p className="muted">
                  Fehlen noch: {calibration.missing_targets.join(", ")}
                </p>
              )}
            </>
          )}
        </>
      )}

      {report && (
        <div className="report">
          <h3>Ergebnis</h3>
          <pre>{report}</pre>
          <div className="actions">
            <button className="primary"
                    onClick={() => run(async () => {
                      const saved = await api.calibSave();
                      setReport(`${report}\n\nGespeichert: ${saved.saved_to}\n${saved.note}`);
                    })}
                    disabled={busy}>
              In calibration.yaml speichern
            </button>
            <button onClick={() => { setReport(null); setMethod("none");
                                     setCorners(null); }}
                    disabled={busy}>
              Neu starten
            </button>
          </div>
        </div>
      )}

      {method !== "none" && <hr />}

      <h3>Servos einstellen</h3>
      <p className="hint">
        Der Stift muss sicher aufsetzen, aber nicht pressen.
      </p>
      <div className="grid2">
        <label>Stift oben
          <input type="number" step={0.5} value={servo?.pen_up ?? 20}
                 onChange={(e) => {
                   const pen_up = +e.target.value;
                   void run(async () => setServo(await api.calibServo({ pen_up })));
                 }} />
        </label>
        <label>Stift unten
          <input type="number" step={0.5} value={servo?.pen_down ?? 0}
                 onChange={(e) => {
                   const pen_down = +e.target.value;
                   void run(async () => setServo(await api.calibServo({ pen_down })));
                 }} />
        </label>
      </div>
      <div className="actions">
        <button onClick={() => run(() => api.calibServo({ test: true }))} disabled={busy}>
          Stift-Hub testen
        </button>
      </div>
      {servo?.problems?.length ? (
        <ul className="warnings">
          {servo.problems.map((problem, index) => <li key={index}>{problem}</li>)}
        </ul>
      ) : null}

      <hr />
      <h3>FluidNC-Konfiguration</h3>
      <p className="hint">
        Vor dem Aufspielen die markierten Schlüssel gegen <code>$CD</code> auf
        dem Controller prüfen.
      </p>
      <div className="actions">
        <a className="download" href={api.fluidncUrl("3.7")} download>Schema 3.7</a>
        <a className="download" href={api.fluidncUrl("3.6")} download>Schema 3.6</a>
      </div>
      <p className="muted">
        Aktuelles Modell: {config?.geometry.kinematics_model ?? "–"}
        {config?.geometry.precompensate ? ", mit Vorverzerrung" : ""}
      </p>
    </div>
  );
}
