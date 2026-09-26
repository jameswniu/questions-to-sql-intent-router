import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { EvidencePanel } from "@/chat/evidence/EvidencePanel";
import type { AnswerBlock, Turn } from "@/chat/turn";
import { TooltipProvider } from "@/components/ui/tooltip";
import { HAIL_SQL, dana, figures, policy, turnFrom, why } from "@/test/fixtures";

function renderPanel(turn: Turn) {
  const answer = turn.blocks.find((block): block is AnswerBlock => block.type === "answer");
  const view = render(
    <TooltipProvider>
      <EvidencePanel
        evidence={turn.evidence}
        stages={turn.stages}
        verifier={turn.verifier}
        me={dana}
        numbers={answer?.numbers ?? new Map<string, number>()}
      />
    </TooltipProvider>,
  );
  const details = view.container.querySelector("details.evidence");
  if (!(details instanceof HTMLDetailsElement)) throw new Error("no evidence panel");
  return { ...view, details };
}

async function open(details: HTMLDetailsElement) {
  const summary = details.querySelector(":scope > summary");
  if (!summary) throw new Error("no summary");
  await userEvent.click(summary);
  expect(details.open).toBe(true);
}

/** The panel whose heading is title, as the demo recorder finds it. */
function section(details: HTMLDetailsElement, title: string): HTMLElement {
  const found = [...details.querySelectorAll<HTMLElement>(":scope > section")].find(
    (box) => box.querySelector("h3")?.textContent === title,
  );
  if (!found) throw new Error(`no ${title} section`);
  return found;
}

describe("the evidence panel", () => {
  it("is folded under a summary that says what it holds", () => {
    const { details } = renderPanel(turnFrom(figures));
    expect(details.open).toBe(false);
    const chips = [...details.querySelectorAll("summary .chip")].map((chip) => chip.textContent);
    expect(chips).toEqual(["SQL", "1 row", "1 kept, 0 cut", "44 ms"]);
  });

  it("opens on the SQL, run as the asker, with its bound values in order", async () => {
    const { details } = renderPanel(turnFrom(figures));
    await open(details);
    const tabs = within(details)
      .getAllByRole("tab")
      .map((tab) => tab.textContent);
    expect(tabs).toEqual(["SQL", "Rows1", "Checks1", "Timings"]);
    const sql = section(details, "SQL");
    expect(sql).toBeVisible();
    expect(sql.querySelector("pre.sql")?.textContent).toBe(HAIL_SQL);
    expect(sql.querySelector(".role-note")).toHaveTextContent(
      "Ran on Dana Reyes's own database login, u_adj_west, so row-level security applied.",
    );
    const params = [...sql.querySelectorAll("ol.params li")].map((item) => item.textContent);
    expect(params).toEqual(["2025-04-01", "2025-06-30", "[CO]", "[hail]"]);
    expect(section(details, "Rows")).not.toBeVisible();
  });

  it("shows the rows a query returned under their own tab", async () => {
    const { details } = renderPanel(turnFrom(figures));
    await open(details);
    await userEvent.click(within(details).getByRole("tab", { name: /Rows/ }));
    const rows = section(details, "Rows");
    expect(rows).toBeVisible();
    expect(within(rows).getByRole("columnheader", { name: "value" })).toBeInTheDocument();
    expect(rows.querySelector("table.rows td.num")?.textContent).toBe("4108452.79");
    expect(section(details, "SQL")).not.toBeVisible();
  });

  it("leads a why answer with its driver split, each group's share as the answer names it", async () => {
    const { details } = renderPanel(turnFrom(why));
    await open(details);
    expect(within(details).getAllByRole("tab")[0]).toHaveTextContent("Driver split");
    const split = section(details, "Driver split");
    expect(split.querySelector("p.legend")?.textContent).toMatch(/^Split in the sandbox: the count effect/);
    const hail = within(split).getByRole("rowheader", { name: "hail" }).closest("tr");
    expect(hail?.querySelectorAll("td.num")).toHaveLength(4);
    expect(hail?.querySelector("td:last-child")).toHaveTextContent(/^97\.4%$/);
    expect(split.querySelector("table.split caption")?.textContent).toBe("By peril");
    expect(within(details).getByRole("tab", { name: /SQL/ })).toHaveTextContent("SQL2");
  });

  it("lists the retrieved passages, marking the one the answer cites", async () => {
    const { details } = renderPanel(turnFrom(policy));
    await open(details);
    const passages = section(details, "Retrieved passages");
    const items = passages.querySelectorAll("ul.chunks > li");
    expect(items).toHaveLength(2);
    expect(items[0]?.querySelector(".where")).toHaveTextContent("HO-2025 policy: Homeowners Policy · Flood");
    expect(items[0]).toHaveTextContent("Source 1");
    expect(items[0]?.querySelector(".score")?.textContent).toBe("score 0.873");
    expect(items[1]).not.toHaveTextContent("Source 1");
  });

  it("shows each kept claim, and a cut one only by its reason", async () => {
    const { details } = renderPanel(turnFrom(policy));
    await open(details);
    await userEvent.click(within(details).getByRole("tab", { name: /Checks/ }));
    const checks = section(details, "Verifier");
    expect(checks.querySelector("p.legend")?.textContent).toBe("1 claim kept, 1 cut.");
    const [kept, cut] = [...checks.querySelectorAll("ul.claims > li")];
    expect(kept).toHaveTextContent("KeptFlood damage is excluded under HO-2025.");
    expect(cut).toHaveClass("cut");
    expect(cut).toHaveTextContent("Cut");
  });

  it("draws the stage timings on one timeline with their total", async () => {
    const { details } = renderPanel(turnFrom(figures));
    await open(details);
    await userEvent.click(within(details).getByRole("tab", { name: /Timings/ }));
    const timings = section(details, "Stage timings");
    expect(timings.querySelectorAll(".waterfall > li")).toHaveLength(3);
    expect(timings.querySelector("p.legend")?.textContent).toBe("route 4 ms · sql 38 ms · verify 2 ms · total 44 ms");
  });

  it("is left out when there is no evidence at all", () => {
    const { container } = render(
      <EvidencePanel evidence={[]} stages={[]} verifier={null} me={dana} numbers={new Map()} />,
    );
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("Evidence")).toBeNull();
  });
});
