import { useRef, useState } from "react";
import { api } from "../../api";
import { useStore } from "../../store";

const TRACE_MODES = [
  { value: "contours", label: "Konturen", hint: "Fotos — Canny-Kanten" },
  { value: "bitmap", label: "Silhouette", hint: "Logos, Flächen" },
  { value: "centerline", label: "Mittellinie", hint: "echte Strichzeichnungen" },
  { value: "halftone", label: "Schraffur", hint: "Graustufen als Liniendichte" },
  { value: "stipple", label: "Punkte", hint: "Stippling" },
  { value: "tsp", label: "Eine Linie", hint: "TSP-Art, Stift bleibt unten" },
];

const COLOR_MODES = [
  { value: "single", label: "Einfarbig" },
  { value: "palette", label: "3 Farben (Lab-Quantisierung)" },
  { value: "tonal", label: "Tonwert (Kontur + 2× Schraffur)" },
];

export function ImagePanel() {
  const { pipeline, setPipeline, run, busy, status } = useStore();
  const [traceMode, setTraceMode] = useState("contours");
  const [colorMode, setColorMode] = useState("single");
  const [file, setFile] = useState<File | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const preview = async (nextTrace = traceMode, nextColor = colorMode) => {
    if (!file) return;
    const result = await run(() => api.imagePreview(file, nextTrace, nextColor));
    if (result) setPipeline(result);
  };

  const onFile = (event: React.ChangeEvent<HTMLInputElement>) => {
    const picked = event.target.files?.[0] ?? null;
    setFile(picked);
    if (picked) {
      void run(() => api.imagePreview(picked, traceMode, colorMode))
        .then((result) => result && setPipeline(result));
    }
  };

  const stats = pipeline?.stats;
  const canDraw = !!pipeline && pipeline.n_paths > 0
    && status?.state === "idle" && !busy;

  return (
    <div className="panel">
      <h2>Bild zeichnen</h2>

      <div className="field">
        <label htmlFor="image-file">Bilddatei</label>
        <input id="image-file" ref={inputRef} type="file"
               accept="image/*" onChange={onFile} />
      </div>

      <div className="field">
        <label htmlFor="trace-mode">Verfahren</label>
        <select id="trace-mode" value={traceMode}
                onChange={(e) => { setTraceMode(e.target.value); void preview(e.target.value); }}>
          {TRACE_MODES.map((m) => (
            <option key={m.value} value={m.value}>{m.label} — {m.hint}</option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="color-mode">Farben</label>
        <select id="color-mode" value={colorMode}
                onChange={(e) => { setColorMode(e.target.value); void preview(traceMode, e.target.value); }}>
          {COLOR_MODES.map((m) => (
            <option key={m.value} value={m.value}>{m.label}</option>
          ))}
        </select>
      </div>

      <div className="actions">
        <button onClick={() => preview()} disabled={!file || busy}>
          Vorschau aktualisieren
        </button>
        <button className="primary"
                onClick={() => run(() => api.draw())}
                disabled={!canDraw}
                title={canDraw ? "" : "Erst eine Vorschau erzeugen, Maschine muss bereit sein"}>
          Zeichnen starten
        </button>
      </div>

      {stats && (
        <div className="report">
          <h3>Vorschau</h3>
          <dl>
            <dt>Pfade</dt><dd>{stats.n_strokes}</dd>
            <dt>Punkte</dt><dd>{stats.n_points.toLocaleString("de-DE")}</dd>
            <dt>Zeichenweg</dt><dd>{(stats.draw_length_mm / 1000).toFixed(1)} m</dd>
            <dt>Leerfahrt</dt>
            <dd>
              {(stats.travel_length_mm / 1000).toFixed(1)} m
              {" "}({(stats.travel_ratio * 100).toFixed(0)} %)
            </dd>
            <dt>Farbwechsel</dt><dd>{stats.n_color_changes}</dd>
            <dt>Geschätzte Dauer</dt><dd><b>{stats.total_duration_human}</b></dd>
          </dl>

          {pipeline?.optimize && (
            <p className="muted">
              Optimierung: {pipeline.optimize.paths_before} → {pipeline.optimize.paths_after} Pfade,
              {" "}{pipeline.optimize.merged} verbunden,
              {" "}{pipeline.optimize.dropped_short} zu kurz verworfen,
              {" "}Leerfahrt {pipeline.optimize.travel_saved_percent.toFixed(0)} % kürzer.
            </p>
          )}

          {pipeline?.warnings?.length ? (
            <ul className="warnings">
              {pipeline.warnings.map((warning, index) => <li key={index}>{warning}</li>)}
            </ul>
          ) : null}

          <a className="download" href={api.gcodeUrl()} download>
            G-Code herunterladen
          </a>
        </div>
      )}

      {!file && (
        <p className="muted">
          Lade ein Foto hoch. Die Vorschau zeigt exakt, was gezeichnet würde —
          inklusive Dauer und Farbwechseln, bevor irgendetwas an die Maschine geht.
        </p>
      )}
    </div>
  );
}
