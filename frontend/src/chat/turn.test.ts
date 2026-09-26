import { describe, expect, it } from "vitest";

import { paragraphs, withMarkers } from "@/chat/answer";
import { sqlTokens } from "@/chat/evidence/sqlTokens";
import { applyEvent, closes, newTurn, type AnswerBlock, type NoticeBlock } from "@/chat/turn";
import { HAIL_SQL, figures, policy, turnFrom } from "@/test/fixtures";

describe("a turn built from the stream", () => {
  it("keeps the answer's words exactly and records the verifier's counts", () => {
    const turn = turnFrom(figures);
    const [answer] = turn.blocks as AnswerBlock[];
    expect(answer?.paragraphs).toEqual([
      { kind: "p", pieces: ["We paid $4,108,453 on hail claims in Colorado in Q2 2025."] },
    ]);
    expect(turn.verifier).toMatchObject({ cut: 0, reasons: [] });
    expect(turn.stages.map((stage) => stage.name)).toEqual(["route", "sql", "verify"]);
    expect(turn.done?.route).toBe("quantitative");
    expect(turn.ended && turn.finished).toBe(true);
  });

  it("numbers each cited source once and marks the claim that cites it", () => {
    const [answer] = turnFrom(policy).blocks as AnswerBlock[];
    expect(answer?.citations).toHaveLength(1);
    expect(answer?.paragraphs).toEqual([
      { kind: "p", pieces: ["Flood damage is excluded under HO-2025.", 1] },
      { kind: "ul", items: [["Surface water is excluded too."]] },
    ]);
    expect(answer?.caveats).toHaveLength(1);
  });

  it("offers a question's options with a capital, and fills in what the server sent", () => {
    const turn = applyEvent(newTurn(1, "How much did we pay?", 0), "clarify", {
      question: "For which period?",
      options: ["year to date", { label: "Q2 2025", value: "q2 2025" }],
    });
    const [notice] = turn.blocks as NoticeBlock[];
    expect(notice?.label).toBe("Needs one more detail");
    expect(notice?.options).toEqual([
      { label: "Year to date", fill: "year to date" },
      { label: "Q2 2025", fill: "q2 2025" },
    ]);
  });

  it("adds the range the data covers only when the message doesn't name it", () => {
    const named = applyEvent(newTurn(1, "q", 0), "out_of_data", {
      message: "My data covers January 2024 to June 2026, so 2022 is outside it.",
      covered: "January 2024 to June 2026",
    });
    const unnamed = applyEvent(newTurn(1, "q", 0), "out_of_data", {
      message: "2022 is before my data starts.",
      covered: { start: "January 2024", end: "June 2026" },
    });
    expect((named.blocks[0] as NoticeBlock).extra).toBeNull();
    expect((unnamed.blocks[0] as NoticeBlock).extra).toBe("My data covers January 2024 to June 2026.");
  });

  it("ends the answer on an error or a kind it doesn't know", () => {
    const failed = applyEvent(newTurn(1, "q", 0), "error", { message: "Something went wrong on our side." });
    expect(failed.ended).toBe(true);
    expect(failed.blocks[0]).toMatchObject({ kind: "error", label: "Something went wrong" });
    expect(applyEvent(newTurn(1, "q", 0), "surprise", {}).blocks[0]).toMatchObject({
      message: "The answer could not be finished.",
    });
    expect(["stage", "evidence", "answer", "refused", "clarify", "out_of_data"].some(closes)).toBe(false);
    expect(closes("done") && closes("error") && closes("surprise")).toBe(true);
  });

  it("says part could not be confirmed when the verifier only flags it", () => {
    const turn = applyEvent(newTurn(1, "q", 0), "answer", { text: "Paid $10.", could_not_confirm: true });
    expect((turn.blocks[0] as AnswerBlock).caveats).toEqual(["Part of this could not be confirmed."]);
  });
});

describe("the answer's markers", () => {
  it("turns the server's own [n] markers into links too, and leaves the words alone", () => {
    const marked = withMarkers("See the wording [2].", [], new Map());
    expect(paragraphs(marked)).toEqual([{ kind: "p", pieces: ["See the wording ", 2, "."] }]);
  });
});

describe("SQL highlighting", () => {
  it("gives back the statement character for character", () => {
    const tokens = sqlTokens(HAIL_SQL);
    expect(tokens.map(([, text]) => text).join("")).toBe(HAIL_SQL);
    expect(tokens.filter(([kind]) => kind === "placeholder")).toHaveLength(4);
    expect(tokens.find(([, text]) => text === "SUM")?.[0]).toBe("function");
    const marked = tokens.filter(([kind]) => kind !== "plain").map(([kind, text]) => `${kind}:${text}`);
    expect(marked.slice(0, 6)).toEqual([
      "keyword:SELECT",
      "function:SUM",
      "keyword:AS",
      "keyword:FROM",
      "keyword:WHERE",
      "keyword:BETWEEN",
    ]);
    expect(marked.join(" ")).not.toContain("sem.v_payments_net");
  });
});
