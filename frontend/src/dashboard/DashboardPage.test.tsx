import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import DashboardPage from "@/dashboard/DashboardPage";
import type { DashboardView } from "@/dashboard/types";
import type { SessionView } from "@/lib/types";
import { dana } from "@/test/fixtures";
import { fakeServer, json, renderWithQueries } from "@/test/server";

const priya: SessionView = {
  me: { ...dana, user_id: "priya", name: "Priya Natarajan", title: "Claims supervisor, all regions", ops: true },
  demo: true,
  users: [],
};

function view(source: string | null, requests: number): DashboardView {
  const labels: [string, string | null][] = [
    ["All", null],
    ["UI", "ui"],
    ["Eval", "eval"],
    ["Replay", "replay"],
  ];
  const chart = {
    title: "Requests by route",
    description: `Lookup: ${requests}.`,
    x_label: "Requests",
    ticks: [0, 10, 20].map((value) => ({ value, label: String(value) })),
    bars: [{ label: "Lookup", value: requests, text: String(requests), tone: "accent" as const }],
    budget: null,
  };
  return {
    scope: source ? `Last 30 days, ${source === "eval" ? "Eval" : "UI"} requests only.` : "Last 30 days, all sources.",
    source,
    filters: labels.map(([label, key]) => ({
      label,
      source: key,
      href: key ? `/dashboard?source=${key}` : "/dashboard",
      current: key === source,
    })),
    requests,
    tiles: [{ label: "Requests", value: String(requests) }],
    latency: [],
    by_route: chart,
    outcomes: { ...chart, title: "Outcomes" },
    verifier: { chart: null, note: "No answer has been through the verifier yet." },
    feedback: null,
    cost: { chart: null, note: "Zero so far. Every request ran in no-key mode, which calls no model." },
    evals: null,
  };
}

describe("the dashboard", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/dashboard");
  });

  it("shows the tiles and charts for all sources, then swaps in one source's without reloading", async () => {
    const calls = fakeServer({
      "GET /api/session": () => json(priya),
      "GET /api/dashboard": ({ url }) => (url.endsWith("source=eval") ? json(view("eval", 7)) : json(view(null, 19))),
    });
    const { container } = renderWithQueries(<DashboardPage />);
    const main = container.querySelector("main.dashboard");
    await waitFor(() => {
      expect(main).toHaveAttribute("data-source", "all");
    });
    expect(main).toHaveAttribute("aria-busy", "false");
    expect(container.querySelector(".dashboard > h1")?.textContent).toBe("Service dashboard");
    expect(container.querySelector(".dashboard > p.muted")).toHaveTextContent(
      "Last 30 days, all sources. Read from ops.request_log, ops.feedback and ops.eval_runs.",
    );
    expect(container.querySelector(".tile-value")?.textContent).toBe("19");
    const traffic = screen.getByRole("heading", { name: "Traffic and outcomes" }).closest("section.panel");
    expect(traffic?.querySelectorAll("svg.chart")).toHaveLength(2);
    expect(screen.getByText("No eval runs yet.")).toBeVisible();

    const filters = screen.getByRole("navigation", { name: "Requests from" });
    await userEvent.click(within(filters).getByRole("link", { name: "Eval" }));
    expect(within(filters).getByRole("link", { name: "Eval" })).toHaveAttribute("aria-current", "page");
    expect(window.location.pathname + window.location.search).toBe("/dashboard?source=eval");
    await waitFor(() => {
      expect(main).toHaveAttribute("data-source", "eval");
    });
    expect(main).toHaveAttribute("aria-busy", "false");
    expect(container.querySelector(".tile-value")?.textContent).toBe("7");
    expect(container.querySelector(".dashboard > p.muted")).toHaveTextContent("Eval requests only.");
    expect(calls.map((call) => call.url)).toContain("/api/dashboard?source=eval");
  });

  it("says there are no requests yet on an empty window", async () => {
    fakeServer({
      "GET /api/session": () => json(priya),
      "GET /api/dashboard": () => json({ ...view(null, 0), by_route: null, outcomes: null }),
    });
    renderWithQueries(<DashboardPage />);
    expect(await screen.findByRole("heading", { name: "No requests yet" })).toBeVisible();
    expect(document.querySelector(".tiles")).toBeNull();
  });

  it("shows the server's sentence when the data can't be read", async () => {
    fakeServer({
      "GET /api/session": () => json(priya),
      "GET /api/dashboard": () => new Response("The dashboard is for operators only.", { status: 403 }),
    });
    renderWithQueries(<DashboardPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The dashboard is for operators only.");
  });
});
