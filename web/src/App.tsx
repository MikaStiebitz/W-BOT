import { useEffect } from "react";
import { api, ws } from "./api";
import { AmsPanel } from "./components/AmsPanel";
import { BoardView } from "./components/BoardView";
import { StatusBar } from "./components/StatusBar";
import { CalibrationPanel } from "./components/panels/CalibrationPanel";
import { ImagePanel } from "./components/panels/ImagePanel";
import { LivePanel } from "./components/panels/LivePanel";
import { ManualPanel } from "./components/panels/ManualPanel";
import { useStore } from "./store";
import type { Mode } from "./types";

const MODES: { value: Mode; label: string }[] = [
  { value: "image", label: "Bild" },
  { value: "live", label: "Live malen" },
  { value: "calibrate", label: "Kalibrieren" },
  { value: "manual", label: "Manuell" },
];

export default function App() {
  const { mode, setMode, init, sketchActive } = useStore();

  useEffect(() => {
    void init();
    return () => ws.close();
  }, [init]);

  // The space bar is a global e-stop. It works whatever has focus, except
  // while typing into a field — where it would otherwise be impossible to
  // write a sentence.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.code !== "Space" || event.repeat) return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
          || target?.isContentEditable) {
        return;
      }
      event.preventDefault();
      if (ws.isOpen) ws.send("estop");
      else void useStore.getState().run(() => api.estop());
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="app">
      <StatusBar />
      <main className="layout">
        <section className="board-panel">
          <BoardView />
        </section>
        <aside className="side-panel">
          <AmsPanel />
          <nav className="modes segmented" aria-label="Betriebsart">
            {MODES.map((entry) => (
              <button
                key={entry.value}
                className={mode === entry.value ? "active" : ""}
                onClick={() => setMode(entry.value)}
                disabled={sketchActive && entry.value !== "live"}
                title={sketchActive && entry.value !== "live"
                  ? "Beende zuerst die Live-Session"
                  : undefined}
              >
                {entry.label}
              </button>
            ))}
          </nav>
          {mode === "image" && <ImagePanel />}
          {mode === "live" && <LivePanel />}
          {mode === "calibrate" && <CalibrationPanel />}
          {mode === "manual" && <ManualPanel />}
        </aside>
      </main>
    </div>
  );
}
