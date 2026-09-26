import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Notice } from "@/chat/Notice";
import { applyEvent, newTurn, withNotice, type NoticeBlock } from "@/chat/turn";

function noticeOf(type: string, data: unknown): NoticeBlock {
  const [block] = applyEvent(newTurn(1, "q", 0), type, data).blocks;
  if (block?.type !== "notice") throw new Error(`${type} made no notice`);
  return block;
}

describe("the clarify options", () => {
  it("offers each option as a button that fills in exactly what the server sent", async () => {
    const onFill = vi.fn();
    const notice = noticeOf("clarify", { question: "For which period?", options: ["2025", "year to date"] });
    const { container } = render(<Notice notice={notice} onFill={onFill} />);
    const box = container.querySelector(".notice.clarify");
    expect(box?.querySelector(".label")?.textContent).toBe("Needs one more detail");
    expect(box?.querySelector("p")).toHaveTextContent("For which period?");
    const option = screen.getByRole("button", { name: "Year to date" });
    expect(option).toHaveAttribute("data-fill", "year to date");
    expect(screen.getByRole("button", { name: "2025" }).closest("ul")).toHaveClass("options");
    await userEvent.click(option);
    expect(onFill).toHaveBeenCalledWith("year to date");
  });
});

describe("the notices", () => {
  it("labels a refusal and shows the gate's own message", () => {
    const notice = noticeOf("refused", { reason: "injection", message: "I can only answer questions about claims." });
    const { container } = render(<Notice notice={notice} onFill={vi.fn()} />);
    const box = container.querySelector(".notice.refused");
    expect(box?.querySelector(".label")?.textContent).toBe("Not answered");
    expect(box?.querySelector("p")?.textContent).toBe("I can only answer questions about claims.");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("names the range the data covers when the message leaves it out", () => {
    const notice = noticeOf("out_of_data", {
      message: "2022 is before my data starts.",
      covered: "January 2024 to June 2026",
    });
    const { container } = render(<Notice notice={notice} onFill={vi.fn()} />);
    expect(container.querySelector(".notice.outside .label")?.textContent).toBe("Outside the data");
    expect(container.querySelector(".notice.outside p.muted")).toHaveTextContent(
      "My data covers January 2024 to June 2026.",
    );
  });

  it("shows a request that was never sent as an error with the server's sentence", () => {
    const said = "You have asked 20 questions in the last minute. Wait 3 seconds and try again.";
    const [notice] = withNotice(newTurn(1, "q", 0), "error", "Not sent", said).blocks as NoticeBlock[];
    const { container } = render(<Notice notice={notice!} onFill={vi.fn()} />);
    expect(container.querySelector(".notice.error .label")?.textContent).toBe("Not sent");
    expect(container.querySelector(".notice.error p")?.textContent).toBe(said);
  });
});
