import { Table2 } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState, type RefObject } from "react";

import { Hint } from "@/components/ui/tooltip";
import { BAR, barPath, FALLBACK_WIDTH, LABEL_GAP, layout, ROW, VALUE_GAP } from "@/dashboard/chartLayout";
import { fitText, rootPx, textWidth } from "@/dashboard/measure";
import type { Chart } from "@/dashboard/types";
import { cn } from "@/lib/cn";

/** The element's width, followed as it changes. Before the first measurement the chart draws at a fallback width. */
function useWidth(ref: RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(FALLBACK_WIDTH);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      const next = Math.floor(entry?.contentRect.width ?? 0);
      if (next > 0) setWidth(next);
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, [ref]);
  return width;
}

/** Whether the page's fonts have loaded, so text is measured in the face it is drawn in. */
function useFontsReady(): boolean {
  const [ready, setReady] = useState(() => typeof document.fonts === "undefined" || document.fonts.status === "loaded");
  useEffect(() => {
    if (ready) return;
    let live = true;
    void document.fonts.ready.then(() => {
      if (live) setReady(true);
    });
    return () => {
      live = false;
    };
  }, [ready]);
  return ready;
}

/**
 * A horizontal bar chart drawn at its container's width, so its text keeps one size on any screen. Every bar is
 * labelled with its value; hovering one lifts it and names it in full. The chart's title and description read every
 * bar out to a screen reader.
 */
export function BarChart({ chart }: { chart: Chart }) {
  const id = useId();
  const box = useRef<HTMLDivElement>(null);
  const width = useWidth(box);
  // Once the fonts load, this renders again and the text is measured in the face it is drawn in.
  useFontsReady();
  const [active, setActive] = useState<number | null>(null);
  const shape = layout(chart, width, rootPx());
  const hovered = active === null ? undefined : chart.bars[active];
  const budgetX = chart.budget ? shape.x(chart.budget.value) : 0;
  const barEnd = (value: number) => Math.max(shape.x(value), shape.x0 + (value > 0 ? 2 : 0));
  const budgetHalf = chart.budget ? textWidth(chart.budget.label, shape.tickPx, 500) / 2 : 0;

  return (
    <div ref={box} className="relative w-full">
      <svg
        className="chart"
        width={width}
        height={shape.height}
        viewBox={`0 0 ${width} ${shape.height}`}
        role="img"
        aria-labelledby={`${id}-title ${id}-desc`}
        data-active={active === null ? undefined : ""}
        onPointerLeave={() => {
          setActive(null);
        }}
      >
        <title id={`${id}-title`}>{chart.title}</title>
        <desc id={`${id}-desc`}>{chart.description}</desc>
        {chart.ticks.map((tick, index) => {
          const at = shape.x(tick.value);
          return (
            <g key={`tick-${index}`}>
              <line
                className={tick.value === 0 ? "c-axis" : "c-grid"}
                x1={at}
                x2={at}
                y1={shape.top - 4}
                y2={shape.bottom}
              />
              {index % shape.every === 0 && (
                <text className="c-tick" x={at} y={shape.bottom + 18} textAnchor="middle">
                  {tick.label}
                </text>
              )}
            </g>
          );
        })}
        {chart.bars.map((bar, index) => {
          const y = shape.top + index * ROW;
          const middle = y + ROW / 2;
          const end = barEnd(bar.value);
          const label = fitText(bar.label, shape.labelPx, shape.labelBox);
          return (
            <g
              key={`bar-${index}`}
              className="c-row"
              data-active={active === index ? "" : undefined}
              onPointerEnter={() => {
                setActive(index);
              }}
            >
              <rect className="c-hit" x={0} y={y} width={width} height={ROW} />
              <text className="c-label" x={shape.x0 - LABEL_GAP} y={middle} dy="0.35em" textAnchor="end">
                {label}
              </text>
              {bar.value > 0 && (
                <path className={`tone-${bar.tone}`} d={barPath(shape.x0, end, middle - BAR / 2, BAR)} />
              )}
            </g>
          );
        })}
        {/* The budget line crosses the bars, so a bar past its budget shows it, and stops at each value's label. */}
        {chart.budget && (
          <line
            className="c-budget"
            x1={budgetX}
            x2={budgetX}
            y1={shape.top - 8}
            y2={shape.bottom}
            strokeWidth={1.5}
            strokeDasharray="5 4"
          />
        )}
        <g className="c-values">
          {chart.bars.map((bar, index) => {
            const middle = shape.top + index * ROW + ROW / 2;
            const x = barEnd(bar.value) + VALUE_GAP;
            const backing = textWidth(bar.text, shape.valuePx, 500) + 8;
            return (
              <g key={`value-${index}`} className="c-row" data-active={active === index ? "" : undefined}>
                <rect
                  className="c-value-backing"
                  x={x - 4}
                  y={middle - shape.valuePx * 0.7}
                  width={backing}
                  height={shape.valuePx * 1.4}
                  rx={3}
                />
                <text className="c-value" x={x} y={middle} dy="0.35em">
                  {bar.text}
                </text>
              </g>
            );
          })}
        </g>
        {chart.budget && (
          <text
            className="c-budget-label"
            x={Math.min(Math.max(budgetX, shape.x0 + budgetHalf), width - budgetHalf)}
            y={shape.top - 12}
            textAnchor="middle"
          >
            {chart.budget.label}
          </text>
        )}
        <text className="c-axis-label" x={(shape.x0 + shape.x1) / 2} y={shape.bottom + 42} textAnchor="middle">
          {chart.x_label}
        </text>
      </svg>
      {hovered && active !== null && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute z-10 max-w-[240px] rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs shadow-md"
          style={{
            left: Math.max(0, Math.min(shape.x(hovered.value), width - 200)),
            // Above the hovered bar, clear of its value, or under it for the first bar.
            top: active === 0 ? shape.top + ROW + 2 : shape.top + active * ROW - 54,
          }}
        >
          <div className="font-semibold text-foreground">{hovered.text}</div>
          <div className="text-subtle">{hovered.label}</div>
        </div>
      )}
    </div>
  );
}

/** The chart's numbers as a table, the same values its bars show. */
export function ChartTable({ chart }: { chart: Chart }) {
  return (
    <div className="table-wrap w-full">
      <table className="chart-table">
        <caption className="sr-only">{chart.title}</caption>
        <thead>
          <tr>
            <th scope="col">Bar</th>
            <th scope="col" className="text-right">
              {chart.x_label}
            </th>
          </tr>
        </thead>
        <tbody>
          {chart.bars.map((bar, index) => (
            <tr key={index}>
              <th scope="row">
                <span className="flex items-center gap-2">
                  <span aria-hidden="true" className={cn("size-2.5 shrink-0 rounded-sm", `tone-${bar.tone}`)} />
                  {bar.label}
                </span>
              </th>
              <td className="num">{bar.text}</td>
            </tr>
          ))}
          {chart.budget && (
            <tr>
              <th scope="row">Budget</th>
              <td className="num">{chart.budget.label}</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

/** A chart with its title, and a switch to read it as a table. */
export function ChartFigure({ chart, className }: { chart: Chart; className?: string }) {
  const [table, setTable] = useState(false);
  return (
    <figure className={cn("chart-figure min-w-0", className)}>
      <figcaption className="mb-2 flex items-center justify-between gap-3">
        <span className="chart-title text-sm font-semibold text-foreground">{chart.title}</span>
        <Hint label={table ? "Show the chart" : "Show as a table"}>
          <button
            type="button"
            aria-label={`Table view of ${chart.title}`}
            aria-pressed={table}
            onClick={() => {
              setTable(!table);
            }}
            className="grid size-7 shrink-0 cursor-pointer place-items-center rounded-md text-subtle transition-colors hover:bg-surface-hover hover:text-foreground aria-pressed:bg-surface-hover aria-pressed:text-foreground"
          >
            <Table2 className="size-4" aria-hidden="true" />
          </button>
        </Hint>
      </figcaption>
      {table ? <ChartTable chart={chart} /> : <BarChart chart={chart} />}
    </figure>
  );
}
