import { create } from "zustand";
import { api, ws } from "./api";
import type {
  AmsSlot, BoardConfig, CalibrationState, Heatmap, LiveStatus, Mode,
  PathData, PipelineResult, Point, SketchStrokeInfo, Status,
} from "./types";

interface SketchStrokeView {
  id: number;
  color: number;
  points: Point[];
  status: string;
}

interface AppStore {
  // -- connection ------------------------------------------------------
  config: BoardConfig | null;
  status: Status | null;
  connected: boolean;
  error: string | null;

  // -- marker magazine -------------------------------------------------
  ams: AmsSlot[];

  // -- board view ------------------------------------------------------
  safeArea: number[][][];
  heatmap: Heatmap | null;
  showHeatmap: boolean;
  showSafeArea: boolean;
  showTravel: boolean;
  plannedPaths: PathData[];

  // -- ui --------------------------------------------------------------
  mode: Mode;
  pipeline: PipelineResult | null;
  busy: boolean;

  // -- sketch (async live drawing) -------------------------------------
  sketchActive: boolean;
  sketchColor: number;
  sketchStrokes: Record<number, SketchStrokeView>;
  sketchStats: { queued: number; done: number; failed: number;
                 current_id: number | null; state: string };

  // -- legacy cursor live (kept for API compat) ------------------------
  live: LiveStatus | null;
  liveActive: boolean;

  // -- calibration -----------------------------------------------------
  calibration: CalibrationState | null;

  // -- actions ---------------------------------------------------------
  init: () => Promise<void>;
  setMode: (mode: Mode) => void;
  setError: (error: string | null) => void;
  refreshStatus: () => Promise<void>;
  loadAms: () => Promise<void>;
  loadHeatmap: () => Promise<void>;
  toggleHeatmap: () => void;
  toggleSafeArea: () => void;
  toggleTravel: () => void;
  setPipeline: (result: PipelineResult | null) => void;
  setCalibration: (state: CalibrationState | null) => void;
  setPlannedPaths: (paths: PathData[]) => void;
  setSketchColor: (color: number) => void;
  clearSketchView: () => void;
  run: <T>(action: () => Promise<T>) => Promise<T | undefined>;
}

export const useStore = create<AppStore>((set, get) => ({
  config: null,
  status: null,
  connected: false,
  error: null,

  ams: [],

  safeArea: [],
  heatmap: null,
  showHeatmap: false,
  showSafeArea: true,
  showTravel: true,
  plannedPaths: [],

  mode: "image",
  pipeline: null,
  busy: false,

  sketchActive: false,
  sketchColor: 0,
  sketchStrokes: {},
  sketchStats: { queued: 0, done: 0, failed: 0, current_id: null,
                 state: "stopped" },

  live: null,
  liveActive: false,

  calibration: null,

  async init() {
    try {
      const [config, status, safe, ams] = await Promise.all([
        api.config(), api.status(), api.safeArea(), api.ams(),
      ]);
      set({ config, status, safeArea: safe.polygon, ams: ams.slots,
            connected: status.connected, sketchColor: ams.active ?? 0 });
    } catch (err) {
      set({ error: `Verbindung zum Backend fehlgeschlagen: ${(err as Error).message}` });
    }
    try {
      const sketch = await api.sketchStatus(true);
      if (sketch.active) {
        const strokes: Record<number, SketchStrokeView> = {};
        for (const s of sketch.strokes) {
          if (s.points) strokes[s.id] = { id: s.id, color: s.color,
                                          points: s.points, status: s.status };
        }
        set({ sketchActive: true, sketchStrokes: strokes,
              sketchStats: { queued: sketch.queued, done: sketch.done,
                             failed: sketch.failed,
                             current_id: sketch.current_id,
                             state: sketch.state } });
      }
    } catch { /* no session — fine */ }

    ws.on((frame) => {
      const { kind, payload } = frame;
      if (kind === "hello") {
        const hello = payload as { status: Status; safe_area: number[][][] };
        set({ status: hello.status, safeArea: hello.safe_area, connected: true });
        void get().loadAms();
        return;
      }
      if (kind === "status") {
        const previous = get().status;
        if (!previous) return;
        const p = payload as { x: number; y: number; pen_down: boolean; color: number; feed: number };
        set({
          status: { ...previous, position: { x: p.x, y: p.y },
                    pen_down: p.pen_down, color: p.color, feed: p.feed },
        });
        return;
      }
      if (kind === "ams") {
        const ams = payload as { slots: AmsSlot[] };
        if (ams.slots) set({ ams: ams.slots });
        return;
      }
      if (kind === "state" || kind === "job_started" || kind === "job_finished"
          || kind === "job_cancelled" || kind === "paused" || kind === "resumed"
          || kind === "alarm_cleared" || kind === "connected") {
        void get().refreshStatus();
        return;
      }
      if (kind === "progress") {
        const previous = get().status;
        if (previous) {
          set({ status: { ...previous,
                          progress: payload as unknown as Status["progress"] } });
        }
        return;
      }
      if (kind === "alarm" || kind === "job_failed" || kind === "disconnected"
          || kind === "live_error" || kind === "sketch_error") {
        set({ error: String(payload.message ?? payload.error ?? payload.reason ?? kind) });
        void get().refreshStatus();
        return;
      }
      if (kind === "estop") {
        set({ error: "Not-Halt ausgelöst" });
        void get().refreshStatus();
        return;
      }

      // -- sketch queue --------------------------------------------------
      if (kind === "sketch_started_session") {
        const s = payload as { state?: string };
        set({ sketchActive: true,
              sketchStats: { ...get().sketchStats,
                             state: s.state ?? "ready" } });
        return;
      }
      if (kind === "sketch_stopped") {
        set({ sketchActive: false,
              sketchStats: { ...get().sketchStats, state: "stopped" } });
        return;
      }
      if (kind === "sketch_queued") {
        const stroke = payload.stroke as SketchStrokeInfo;
        if (stroke?.points) {
          set({
            sketchStrokes: {
              ...get().sketchStrokes,
              [stroke.id]: { id: stroke.id, color: stroke.color,
                             points: stroke.points, status: "queued" },
            },
            sketchStats: { ...get().sketchStats,
                           queued: Number(payload.queued ?? 0) },
          });
        }
        return;
      }
      if (kind === "sketch_stroke_started" || kind === "sketch_stroke_done"
          || kind === "sketch_stroke_failed") {
        const id = Number(payload.id);
        const strokes = { ...get().sketchStrokes };
        const status = kind === "sketch_stroke_started" ? "drawing"
          : kind === "sketch_stroke_done" ? "done" : "failed";
        if (strokes[id]) strokes[id] = { ...strokes[id], status };
        const stats = { ...get().sketchStats };
        if (status === "drawing") { stats.current_id = id; stats.state = "drawing"; }
        if (status === "done") { stats.done += 1; stats.current_id = null; }
        if (status === "failed") { stats.failed += 1; stats.current_id = null; }
        if (payload.queued !== undefined) stats.queued = Number(payload.queued);
        if (status !== "drawing" && stats.queued === 0) stats.state = "ready";
        set({ sketchStrokes: strokes, sketchStats: stats });
        return;
      }
      if (kind === "sketch_cleared") {
        const ids = (payload.ids as number[]) ?? [];
        const strokes = { ...get().sketchStrokes };
        for (const id of ids) delete strokes[id];
        set({ sketchStrokes: strokes,
              sketchStats: { ...get().sketchStats, queued: 0 } });
        return;
      }
      if (kind === "sketch_state") {
        const s = payload as unknown as { queued: number; done: number;
          failed: number; current_id: number | null; state: string };
        set({ sketchStats: { queued: s.queued, done: s.done, failed: s.failed,
                             current_id: s.current_id, state: s.state } });
        return;
      }

      // -- legacy cursor live -------------------------------------------
      if (kind === "live_started") {
        set({ liveActive: true, live: payload as unknown as LiveStatus });
        return;
      }
      if (kind === "live_stopped") {
        set({ liveActive: false, live: payload as unknown as LiveStatus });
        return;
      }
      if (kind === "live_status") {
        set({ live: payload as unknown as LiveStatus });
      }
    });
    ws.connect();

    window.setInterval(() => {
      if (!ws.isOpen) void get().refreshStatus();
    }, 3000);
  },

  setMode: (mode) => set({ mode }),
  setError: (error) => set({ error }),

  async refreshStatus() {
    try {
      const status = await api.status();
      set({ status, connected: status.connected });
    } catch {
      set({ connected: false });
    }
  },

  async loadAms() {
    try {
      const ams = await api.ams();
      set({ ams: ams.slots });
    } catch { /* backend gone; the status poll will notice */ }
  },

  async loadHeatmap() {
    if (get().heatmap) return;
    try {
      set({ heatmap: await api.heatmap(25) });
    } catch (err) {
      set({ error: (err as Error).message });
    }
  },

  toggleHeatmap() {
    const next = !get().showHeatmap;
    set({ showHeatmap: next });
    if (next) void get().loadHeatmap();
  },
  toggleSafeArea: () => set({ showSafeArea: !get().showSafeArea }),
  toggleTravel: () => set({ showTravel: !get().showTravel }),

  setPipeline: (pipeline) =>
    set({ pipeline, plannedPaths: pipeline?.paths ?? [] }),
  setCalibration: (calibration) => set({ calibration }),
  setPlannedPaths: (plannedPaths) => set({ plannedPaths }),
  setSketchColor: (sketchColor) => set({ sketchColor }),
  clearSketchView: () => set({
    sketchStrokes: {},
    sketchStats: { queued: 0, done: 0, failed: 0, current_id: null,
                   state: "stopped" },
  }),

  async run(action) {
    set({ busy: true, error: null });
    try {
      return await action();
    } catch (err) {
      set({ error: (err as Error).message });
      return undefined;
    } finally {
      set({ busy: false });
    }
  },
}));
