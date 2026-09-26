import { textWidth } from "@/dashboard/measure";
import type { Chart } from "@/dashboard/types";

// Where everything in a bar chart goes, worked out from its data, its width and the root font size.

export const ROW = 34; // one bar's row
export const BAR = 16; // a bar's thickness, well under the 24px cap, so the rest of its row is air
const RADIUS = 4; // the bar's data end is rounded, and the end on the axis stays square
const TOP = 6;
const BUDGET_ROOM = 26; // above the bars, for the budget line's label
const AXIS = 52; // under the bars: tick labels, then what the axis measures
export const LABEL_GAP = 12;
export const VALUE_GAP = 8;
export const FALLBACK_WIDTH = 640;

/** The chart's geometry: the plot's edges, each value's x, and how often a tick is labelled. */
export function layout(chart: Chart, width: number, root: number) {
  const labelPx = root * 0.8333;
  const valuePx = root * 0.7778;
  const tickPx = root * 0.7222;
  const longest = Math.max(0, ...chart.bars.map((bar) => textWidth(bar.label, labelPx)));
  const labelColumn = Math.round(Math.min(longest + LABEL_GAP, width * 0.42));
  const valueColumn =
    Math.ceil(Math.max(0, ...chart.bars.map((bar) => textWidth(bar.text, valuePx, 500)))) + VALUE_GAP + 4;
  const x0 = labelColumn;
  const x1 = Math.max(x0 + 48, width - valueColumn);
  const top = chart.budget ? BUDGET_ROOM : TOP;
  const bottom = top + ROW * chart.bars.length;
  const max = chart.ticks.at(-1)?.value ?? 1;
  const x = (value: number) => x0 + ((x1 - x0) * Math.max(value, 0)) / (max || 1);
  // Tick labels are thinned to every second tick, or third, and so on, until no two that are shown touch.
  const spans = chart.ticks.map((tick) => {
    const half = textWidth(tick.label, tickPx) / 2;
    return [x(tick.value) - half, x(tick.value) + half] as const;
  });
  const clear = (step: number) =>
    spans.every(([left], index) => index < step || index % step !== 0 || left >= (spans[index - step]?.[1] ?? 0) + 8);
  const every =
    Array.from({ length: Math.max(chart.ticks.length, 1) }, (_, i) => i + 1).find(clear) ?? chart.ticks.length;
  return {
    x0,
    x1,
    top,
    bottom,
    height: bottom + AXIS,
    x,
    labelPx,
    valuePx,
    tickPx,
    labelBox: labelColumn - LABEL_GAP,
    every,
  };
}

/** A bar with a rounded data end and a square end on the axis it grows from. */
export function barPath(from: number, to: number, y: number, height: number): string {
  const r = Math.min(RADIUS, to - from, height / 2);
  return `M${from},${y}H${to - r}A${r},${r} 0 0 1 ${to},${y + r}V${y + height - r}A${r},${r} 0 0 1 ${to - r},${y + height}H${from}Z`;
}
