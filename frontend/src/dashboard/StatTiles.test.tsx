import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatTiles } from "@/dashboard/StatTiles";

describe("the dashboard tiles", () => {
  it("shows each headline number under its label, in the server's order and wording", () => {
    const tiles = [
      { label: "Requests", value: "1,284" },
      { label: "Answered", value: "77%" },
      { label: "Median first event", value: "85 ms" },
      { label: "Rated useful", value: "12 of 15" },
    ];
    const { container } = render(<StatTiles tiles={tiles} />);
    const labels = [...container.querySelectorAll(".tiles .tile-label")].map((node) => node.textContent);
    const values = [...container.querySelectorAll(".tiles .tile-value")].map((node) => node.textContent);
    expect(labels).toEqual(["Requests", "Answered", "Median first event", "Rated useful"]);
    expect(values).toEqual(["1,284", "77%", "85 ms", "12 of 15"]);
  });

  it("says so plainly when there is nothing to count yet", () => {
    const { container } = render(<StatTiles tiles={[{ label: "Rated useful", value: "No ratings yet" }]} />);
    expect(container.querySelector(".tile-value")?.textContent).toBe("No ratings yet");
  });
});
