import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { ChartFigure } from "@/dashboard/BarChart";
import { layout } from "@/dashboard/chartLayout";
import type { Chart } from "@/dashboard/types";

const latency: Chart = {
  title: "Lookup, 40 requests",
  description:
    "First event, p50: 90 ms. First event, p95: 300 ms. Total, p50: 420 ms. Total, p95: 1.60 s. p95 budget 2 s.",
  x_label: "Seconds",
  ticks: [0, 0.5, 1, 1.5, 2].map((value) => ({ value, label: `${value} s` })),
  bars: [
    { label: "First event, p50", value: 0.09, text: "90 ms", tone: "accent-light" },
    { label: "First event, p95", value: 0.3, text: "300 ms", tone: "accent-light" },
    { label: "Total, p50", value: 0.42, text: "420 ms", tone: "accent" },
    { label: "Total, p95", value: 1.6, text: "1.60 s", tone: "accent" },
  ],
  budget: { value: 2, label: "p95 budget 2 s" },
};

function renderFigure(chart: Chart) {
  return render(
    <TooltipProvider>
      <ChartFigure chart={chart} />
    </TooltipProvider>,
  );
}

describe("a dashboard chart", () => {
  it("draws a labelled bar per value, the budget line, and reads it all out", () => {
    const { container } = renderFigure(latency);
    const svg = container.querySelector("svg.chart");
    expect(svg).toHaveAttribute("role", "img");
    expect(svg?.querySelector("title")?.textContent).toBe("Lookup, 40 requests");
    expect(svg?.querySelector("desc")?.textContent).toBe(latency.description);
    expect(svg?.querySelectorAll("path.tone-accent")).toHaveLength(2);
    expect(svg?.querySelectorAll("path.tone-accent-light")).toHaveLength(2);
    const values = [...(svg?.querySelectorAll("text.c-value") ?? [])].map((node) => node.textContent);
    expect(values).toEqual(["90 ms", "300 ms", "420 ms", "1.60 s"]);
    expect(svg?.querySelector("text.c-budget-label")?.textContent).toBe("p95 budget 2 s");
    expect(svg?.querySelector("line.c-budget")).toHaveAttribute("stroke-dasharray", "5 4");
  });

  it("draws no bar for a zero, and still labels it", () => {
    const empty: Chart = {
      ...latency,
      budget: null,
      bars: [{ label: "Claims cut", value: 0, text: "0", tone: "bad" }],
    };
    const { container } = renderFigure(empty);
    expect(container.querySelectorAll("svg.chart path")).toHaveLength(0);
    expect(container.querySelector("text.c-value")?.textContent).toBe("0");
  });

  it("reads as a table with the same numbers", async () => {
    renderFigure(latency);
    await userEvent.click(screen.getByRole("button", { name: "Table view of Lookup, 40 requests" }));
    const table = screen.getByRole("table", { name: "Lookup, 40 requests" });
    expect(screen.getByRole("rowheader", { name: "Total, p95" }).closest("tr")).toHaveTextContent("1.60 s");
    expect(table).toHaveTextContent("p95 budget 2 s");
    expect(document.querySelector("svg.chart")).toBeNull();
  });

  it("thins crowded tick labels to a regular step, so no two shown ones touch", () => {
    const cost: Chart = {
      ...latency,
      budget: null,
      ticks: [0, 0.01, 0.02, 0.03].map((value) => ({ value, label: `$${value.toFixed(4)}` })),
      bars: [
        { label: "Why", value: 0.0257, text: "$0.0257", tone: "accent" },
        { label: "Refused at the gate", value: 0, text: "$0.0000", tone: "accent" },
      ],
    };
    const wide = layout(cost, 900, 18);
    // The cost chart's width in a third of the dashboard at 1280px, where its last two ticks collided.
    const narrow = layout(cost, 310, 18);
    expect(wide.every).toBe(1);
    expect(narrow.every).toBeGreaterThan(1);
    const shown = cost.ticks.filter((_, index) => index % narrow.every === 0).map((tick) => narrow.x(tick.value));
    const gaps = shown.slice(1).map((at, index) => at - (shown[index] ?? 0));
    expect(Math.min(...gaps)).toBeGreaterThan("$0.0000".length * 13 * 0.56 + 8);
  });
});
