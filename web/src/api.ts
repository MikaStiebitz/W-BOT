import type {
  AmsState, BoardConfig, CalibrationState, CornerState, Heatmap, LiveStatus,
  LiveStroke, PipelineResult, SketchStatus, Status, WsFrame,
} from "./types";

const BASE = "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(BASE + path, {
    headers: init?.body instanceof FormData
      ? undefined
      : { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* the body was not JSON; the status line is all we have */
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  config: () => request<BoardConfig>("/api/config"),
  status: () => request<Status>("/api/status"),
  safeArea: () => request<{ polygon: number[][][]; bounds: number[]; summary: Record<string, unknown> }>("/api/safe-area"),
  heatmap: (step = 25) => request<Heatmap>(`/api/heatmap?step_mm=${step}`),

  connect: () => post<Status>("/api/connect"),
  disconnect: () => post<Status>("/api/disconnect"),
  estop: () => post<Status>("/api/estop"),
  clearAlarm: () => post<Status>("/api/clear-alarm"),
  home: () => post<Status>("/api/home"),
  pause: () => post<Status>("/api/pause"),
  resume: () => post<Status>("/api/resume"),
  cancel: () => post<Status>("/api/cancel"),
  resumeJob: () => post<Status>("/api/resume-job"),

  move: (x: number, y: number, draw = false) => post<Status>("/api/move", { x, y, draw }),
  pen: (down: boolean) => post<Status>("/api/pen", { down }),
  color: (index: number) => post<Status>("/api/color", { index }),

  imagePreview: (file: File, traceMode: string, colorMode: string) => {
    const form = new FormData();
    form.append("file", file);
    return request<PipelineResult>(
      `/api/image/preview?trace_mode=${encodeURIComponent(traceMode)}&color_mode=${encodeURIComponent(colorMode)}`,
      { method: "POST", body: form },
    );
  },
  draw: () => post<Status>("/api/draw"),
  gcodeUrl: () => `${BASE}/api/gcode`,
  fluidncUrl: (schema = "3.7") => `${BASE}/api/fluidnc-config?schema=${schema}`,

  liveStart: () => post<LiveStatus>("/api/live/start"),
  liveStop: () => post<LiveStatus>("/api/live/stop"),
  liveStatus: () => request<LiveStatus>("/api/live/status"),
  liveStrokes: () => request<{ strokes: LiveStroke[] }>("/api/live/strokes"),

  calibBegin: () => post<CalibrationState>("/api/calibration/begin"),
  calibSkipReference: () => post<CalibrationState>("/api/calibration/skip-reference"),
  calibReference: (x: number, y: number, left_mm: number, right_mm: number) =>
    post<CalibrationState>("/api/calibration/reference", { x, y, left_mm, right_mm }),
  calibPattern: (n_x: number, n_y: number) =>
    post<CalibrationState & { paths: PipelineResult["paths"] }>("/api/calibration/pattern", { n_x, n_y }),
  calibDraw: () => post<Status>("/api/calibration/draw"),
  calibMeasure: (measurements: { index: number; x: number; y: number }[]) =>
    post<CalibrationState>("/api/calibration/measure", measurements),
  calibSolve: () => post<{ result: Record<string, unknown>; report: string; meets_target: boolean }>("/api/calibration/solve"),
  calibSave: () => post<{ saved_to: string; note: string }>("/api/calibration/save"),
  calibServo: (body: { pen_up?: number; pen_down?: number; revolver?: number[]; test?: boolean }) =>
    post<CalibrationState["servo"]>("/api/calibration/servo", body),

  ams: () => request<AmsState>("/api/ams"),
  amsSetSlot: (body: { index: number; name?: string; hex?: string; enabled?: boolean }) =>
    request<AmsState>("/api/ams/slot", { method: "PUT", body: JSON.stringify(body) }),
  amsSelect: (index: number) => post<AmsState>("/api/ams/select", { index }),

  sketchStart: () => post<SketchStatus>("/api/sketch/start"),
  sketchStop: () => post<SketchStatus>("/api/sketch/stop"),
  sketchStatus: (includePoints = false) =>
    request<SketchStatus>(`/api/sketch/status?include_points=${includePoints}`),
  sketchClear: () => post<SketchStatus & { removed: number[] }>("/api/sketch/clear"),

  cornersBegin: () =>
    post<CalibrationState & { corners: CornerState }>("/api/calibration/corners/begin"),
  cornersMove: (x: number, y: number) =>
    post<{ target: [number, number] }>("/api/calibration/corners/move", { x, y }),
  cornersRecord: (index: number) =>
    post<CalibrationState & { corners: CornerState }>("/api/calibration/corners/record", { index }),
  cornersSolve: () =>
    post<{ result: Record<string, unknown>; report: string; meets_target: boolean }>(
      "/api/calibration/corners/solve"),
};

/**
 * WebSocket with automatic reconnection.
 *
 * The socket carries status at ~12 Hz and, during a live session, cursor
 * updates at up to 60 Hz. Reconnection backs off so a server restart does not
 * turn into a request storm.
 */
export class WsClient {
  private socket: WebSocket | null = null;
  private handlers = new Set<(frame: WsFrame) => void>();
  private reconnectDelay = 500;
  private closedByUs = false;
  private timer: number | null = null;

  connect(): void {
    this.closedByUs = false;
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${location.host}/ws`);
    this.socket = socket;

    socket.onopen = () => { this.reconnectDelay = 500; };
    socket.onmessage = (event) => {
      let frame: WsFrame;
      try {
        frame = JSON.parse(event.data);
      } catch {
        return;
      }
      this.handlers.forEach((handler) => handler(frame));
    };
    socket.onclose = () => {
      this.socket = null;
      if (this.closedByUs) return;
      this.timer = window.setTimeout(() => this.connect(), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, 10_000);
    };
    socket.onerror = () => socket.close();
  }

  close(): void {
    this.closedByUs = true;
    if (this.timer !== null) window.clearTimeout(this.timer);
    this.socket?.close();
    this.socket = null;
  }

  on(handler: (frame: WsFrame) => void): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  send(kind: string, payload: Record<string, unknown> = {}): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ kind, payload }));
    }
  }

  get isOpen(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
  }
}

export const ws = new WsClient();
