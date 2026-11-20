export type MachineState =
  | "disconnected" | "idle" | "running" | "paused"
  | "alarm" | "estop" | "homing" | "jogging";

export type Mode = "image" | "live" | "calibrate" | "manual";

export interface Progress {
  lines_done: number;
  lines_total: number;
  stroke_index: number | null;
  strokes_total: number;
  percent: number;
  seconds_done: number;
  seconds_total: number;
  eta_s: number;
  elapsed_s: number;
  title: string;
}

export interface Status {
  state: MachineState;
  connected: boolean;
  transport: string;
  position: { x: number; y: number };
  controller_state: string;
  pen_down: boolean;
  color: number;
  color_name: string;
  feed: number;
  progress: Progress;
  last_error: string | null;
  can_resume: boolean;
  resume_from_stroke: number;
  pending_lines: number;
}

export interface ColorInfo {
  index: number;
  slot: number;
  name: string;
  hex: string;
}

export interface BoardConfig {
  board: { width_mm: number; height_mm: number };
  geometry: {
    anchor_left: [number, number];
    anchor_right: [number, number];
    pulley_radius_mm: number;
    kinematics_model: string;
    precompensate: boolean;
  };
  colors: ColorInfo[];
  motion: {
    travel_feed_mm_min: number;
    draw_feed_mm_min: number;
    park_position: [number, number];
  };
  marker_line_width_mm: number;
  transport: string;
}

export type Point = [number, number];

export interface PathData {
  color: number;
  closed: boolean;
  points: Point[];
}

export interface Heatmap {
  x: number[];
  y: number[];
  error_amplification: number[][];
  tension_left: number[][];
  tension_right: number[][];
  min_tension: number[][];
  feasible: boolean[][];
}

export interface LiveStatus {
  state: string;
  pen_down: boolean;
  color: number;
  cursor: Point | null;
  actual: Point;
  pending: Point[];
  n_strokes: number;
  blocked_reason: string | null;
  latency_ms: number;
  latency_ok: boolean;
  queue_depth: number;
  undo_supported: boolean;
  last_error: string | null;
}

export interface LiveStroke {
  color: number;
  points: Point[];
  length_mm: number;
}

export interface PipelineStats {
  n_strokes: number;
  n_points: number;
  n_color_changes: number;
  draw_length_mm: number;
  travel_length_mm: number;
  total_length_mm: number;
  travel_ratio: number;
  total_duration_s: number;
  total_duration_human: string;
  colors_used: number[];
}

export interface PipelineResult {
  stats: PipelineStats;
  n_paths: number;
  source_size_px: [number, number];
  elapsed_s: number;
  warnings: string[];
  preview_svg: string;
  paths: PathData[];
  optimize?: {
    paths_before: number;
    paths_after: number;
    travel_saved_percent: number;
    merged: number;
    dropped_short: number;
  } | null;
}

export interface CalibrationTarget {
  index: number;
  nominal_x: number;
  nominal_y: number;
  label: string;
}

export interface CalibrationState {
  step: string;
  has_reference: boolean;
  pattern: { targets: CalibrationTarget[]; rectangle: number[] } | null;
  instructions: string[];
  measurements: {
    index: number;
    commanded: Point;
    measured: Point;
    error_mm: number;
  }[];
  missing_targets: number[];
  result: {
    model: string;
    mean_error_mm: number;
    max_error_mm: number;
    rms_error_mm: number;
    report: string;
    warnings: string[];
  } | null;
  servo: {
    pen_up: number;
    pen_down: number;
    revolver: number[];
    problems: string[];
  };
  errors: string[];
}

export interface WsFrame {
  kind: string;
  payload: Record<string, unknown>;
  t: number;
}

export interface AmsSlot {
  index: number;
  name: string;
  hex: string;
  enabled: boolean;
  rgb: number[];
}

export interface AmsState {
  slots: AmsSlot[];
  enabled: number[];
  active: number;
}

export type SketchStrokeStatus = "queued" | "drawing" | "done" | "failed";

export interface SketchStrokeInfo {
  id: number;
  color: number;
  status: SketchStrokeStatus;
  n_points: number;
  length_mm: number;
  error?: string | null;
  points?: Point[];
}

export interface SketchStatus {
  active: boolean;
  state: string;
  queued: number;
  done: number;
  failed: number;
  current_id: number | null;
  total: number;
  strokes: SketchStrokeInfo[];
  last_error: string | null;
  undo_supported: boolean;
}

export interface CornerState {
  targets: Point[];
  labels: string[];
  recorded: number[];
  missing: number[];
}
