import { paragraphs, withMarkers, type Paragraph } from "@/chat/answer";
import { sourceKey, uniqueSources } from "@/lib/sources";
import { asText as text } from "@/lib/text";
import type { AnswerEvent, Claim, DoneEvent, EvidenceEvent, Source, StageEvent } from "@/lib/types";

// One question and everything its answer's stream brought back, built up one event at a time by applyEvent. The
// wording of every notice is the app's own, and an answer's words are shown exactly as they arrived.

export type NoticeKind = "refused" | "error" | "outside" | "clarify";

export interface Option {
  /** What the button says, starting with a capital so "year to date" reads like "Q2 2026" beside it. */
  label: string;
  /** What choosing it puts in the question box, exactly as the server sent it. */
  fill: string;
}

export interface NoticeBlock {
  type: "notice";
  kind: NoticeKind;
  label: string;
  message: string;
  options: Option[];
  /** A second line, such as the range the data covers when the message doesn't say it. */
  extra: string | null;
}

export interface AnswerBlock {
  type: "answer";
  paragraphs: Paragraph[];
  caveats: string[];
  citations: Source[];
  /** The number each cited source goes by, keyed as sourceKey keys it. */
  numbers: Map<string, number>;
}

export type Block = NoticeBlock | AnswerBlock;

export interface Verifier {
  kept: (Claim | string)[];
  cut: number;
  reasons: string[];
}

export interface Turn {
  id: number;
  question: string;
  startedAt: number;
  stages: StageEvent[];
  evidence: EvidenceEvent[];
  blocks: Block[];
  verifier: Verifier | null;
  done: DoneEvent | null;
  /** The stream closed the way the server closes it, with a done or an error. */
  ended: boolean;
  /** The request is over, however it ended: the evidence and the footer can be shown. */
  finished: boolean;
  finishedAt: number | null;
}

export function newTurn(id: number, question: string, now: number): Turn {
  return {
    id,
    question,
    startedAt: now,
    stages: [],
    evidence: [],
    blocks: [],
    verifier: null,
    done: null,
    ended: false,
    finished: false,
    finishedAt: null,
  };
}

const record = (data: unknown): Record<string, unknown> =>
  typeof data === "object" && data !== null ? (data as Record<string, unknown>) : {};

export function withNotice(
  turn: Turn,
  kind: NoticeKind,
  label: string,
  message: string,
  extras: Partial<Pick<NoticeBlock, "options" | "extra">> = {},
): Turn {
  const notice: NoticeBlock = { type: "notice", kind, label, message, options: [], extra: null, ...extras };
  return { ...turn, blocks: [...turn.blocks, notice] };
}

export function options(raw: unknown): Option[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((option: unknown) => {
    const entry = record(option);
    const label = typeof option === "string" ? option : text(entry.label ?? entry.value);
    const fill = typeof option === "string" ? option : text(entry.value ?? label);
    return { label: label.charAt(0).toUpperCase() + label.slice(1), fill };
  });
}

function answered(turn: Turn, data: AnswerEvent): Turn {
  const citations = uniqueSources(Array.isArray(data.citations) ? data.citations : []);
  const numbers = new Map(citations.map((source, i) => [sourceKey(source, i), i + 1]));
  const kept = Array.isArray(data.claims_kept) ? data.claims_kept : [];
  const claims = kept.filter((claim): claim is Claim => typeof claim === "object");
  const marked = withMarkers(text(data.text), claims, numbers);
  const reasons = Array.isArray(data.could_not_confirm)
    ? data.could_not_confirm.map(text)
    : data.could_not_confirm
      ? ["Part of this could not be confirmed."]
      : [];
  const cut =
    typeof data.claims_cut === "number" ? data.claims_cut : Array.isArray(data.claims_cut) ? data.claims_cut.length : 0;
  const block: AnswerBlock = { type: "answer", paragraphs: paragraphs(marked), caveats: reasons, citations, numbers };
  return { ...turn, blocks: [...turn.blocks, block], verifier: { kept, cut, reasons } };
}

function outside(turn: Turn, data: Record<string, unknown>): Turn {
  const message = text(data.message);
  const range = record(data.covered);
  const covered =
    typeof data.covered === "string"
      ? data.covered
      : range.start && range.end
        ? `${text(range.start)} to ${text(range.end)}`
        : "";
  // The message names the range the data covers, so the range is added only to one that doesn't.
  const extra = covered && !message.includes(covered) ? `My data covers ${covered}.` : null;
  return withNotice(turn, "outside", "Outside the data", message, { extra });
}

const OPEN_EVENTS = new Set(["stage", "evidence", "answer", "refused", "clarify", "out_of_data"]);

/** Whether an event closes the answer: a done, an error, or a kind this page doesn't know. */
export function closes(type: string): boolean {
  return !OPEN_EVENTS.has(type);
}

/** The turn with one more event from the stream applied. */
export function applyEvent(turn: Turn, type: string, payload: unknown): Turn {
  const data = record(payload);
  switch (type) {
    case "stage":
      return { ...turn, stages: [...turn.stages, { name: text(data.name), ms: Number(data.ms) || 0 }] };
    case "evidence":
      return { ...turn, evidence: [...turn.evidence, { kind: text(data.kind), payload: data.payload }] };
    case "answer":
      return answered(turn, data);
    case "refused":
      return withNotice(turn, "refused", "Not answered", text(data.message));
    case "clarify":
      return withNotice(turn, "clarify", "Needs one more detail", text(data.question ?? data.message), {
        options: options(data.options),
      });
    case "out_of_data":
      return outside(turn, data);
    case "done":
      return { ...turn, done: data as unknown as DoneEvent, ended: true };
    default:
      // An error, or a kind this page doesn't know, ends the answer.
      return withNotice(
        { ...turn, ended: true },
        "error",
        "Something went wrong",
        text(data.message) || "The answer could not be finished.",
      );
  }
}

export function finish(turn: Turn, now: number): Turn {
  return { ...turn, finished: true, finishedAt: now };
}

/** The progress line while an answer streams in, as the steps finish. */
export function progressText(turn: Turn): string {
  return turn.stages.length ? "Working." : "Sent. Waiting for the first step.";
}
