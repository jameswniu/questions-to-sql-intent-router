import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AnswerBody } from "@/chat/AnswerBody";
import type { AnswerBlock } from "@/chat/turn";
import { figures, policy, turnFrom } from "@/test/fixtures";

function renderAnswer(events: [string, unknown][], turnId = 3) {
  const turn = turnFrom(events, turnId);
  const block = turn.blocks.find((item): item is AnswerBlock => item.type === "answer");
  if (!block) throw new Error("no answer in these events");
  const { container } = render(
    <section className="answer">
      <AnswerBody block={block} turnId={turnId} />
    </section>,
  );
  return container;
}

describe("the answer card", () => {
  it("shows the answer's own words, one paragraph per line", () => {
    const container = renderAnswer(figures);
    const text = container.querySelector(".answer > .answer-text");
    expect(text?.querySelectorAll(":scope > p")).toHaveLength(1);
    expect(text).toHaveTextContent("We paid $4,108,453 on hail claims in Colorado in Q2 2025.");
    expect(container.querySelector("ol.citations")).toBeNull();
  });

  it("links each marker to its numbered source, which is the link's target", () => {
    const container = renderAnswer(policy, 3);
    const marker = screen.getByRole("link", { name: "Source 1" });
    expect(marker).toHaveAttribute("href", "#cite-3-1");
    expect(marker.closest("sup")).toHaveClass("cite");
    const entry = container.querySelector("#cite-3-1");
    expect(entry?.closest("ol")).toHaveClass("citations");
    expect(entry).toHaveTextContent("HO-2025 policy: Homeowners Policy · Flood");
    expect(container.querySelectorAll(".answer > .answer-text > ul > li")).toHaveLength(1);
  });

  it("shows what the verifier could not confirm under the answer", () => {
    const container = renderAnswer(policy);
    const caveat = container.querySelector(".answer > p.caveat");
    expect(caveat).toHaveTextContent(
      "One statement cited a source that wasn't among the documents found for you, so I left it out.",
    );
  });
});
