// The dashboard as /api/dashboard sends it, from app/web/dashboard.py's Page. Every number arrives worded and
// formatted, so the page, its charts and their table views show the same ones.

export type Tone = "accent" | "accent-light" | "good" | "bad" | "neutral";

export interface Tick {
  value: number;
  label: string;
}

export interface Bar {
  label: string;
  value: number;
  /** The value as the dashboard writes it, such as "1.60 s". */
  text: string;
  tone: Tone;
}

export interface Chart {
  title: string;
  description: string;
  x_label: string;
  ticks: Tick[];
  bars: Bar[];
  budget: { value: number; label: string } | null;
}

export interface Tile {
  label: string;
  value: string;
}

export interface Filter {
  label: string;
  source: string | null;
  href: string;
  current: boolean;
}

export interface Noted {
  chart: Chart | null;
  note: string;
}

export interface EvalTable {
  runs: { split: string; mode: string; date: string; commit: string | null }[];
  rows: { metric: string; values: string[] }[];
}

export interface DashboardView {
  scope: string;
  source: string | null;
  filters: Filter[];
  requests: number;
  tiles: Tile[];
  latency: Chart[];
  by_route: Chart | null;
  outcomes: Chart | null;
  verifier: Noted;
  feedback: Chart | null;
  cost: Noted;
  evals: EvalTable | null;
}
