// Stream events shaped like the pipeline's, as app/web/stream.py encodes them, for the component tests.
import { applyEvent, finish, newTurn, type Turn } from "@/chat/turn";
import type { Me } from "@/lib/types";

export const dana: Me = {
  user_id: "dana",
  name: "Dana Reyes",
  title: "Claims adjuster, West",
  db_role: "u_adj_west",
  ops: false,
};

export const HAIL_SQL =
  "SELECT SUM(amount) AS value FROM sem.v_payments_net WHERE (paid_date BETWEEN %s AND %s) AND state = ANY(%s) AND peril = ANY(%s)";

export const figures: [string, unknown][] = [
  ["stage", { name: "route", ms: 4 }],
  ["stage", { name: "sql", ms: 38.4 }],
  ["evidence", { kind: "sql", payload: { sql: HAIL_SQL, params: ["2025-04-01", "2025-06-30", ["CO"], ["hail"]] } }],
  ["evidence", { kind: "rows", payload: [{ value: "4108452.79" }] }],
  ["stage", { name: "verify", ms: 2 }],
  [
    "answer",
    {
      text: "We paid $4,108,453 on hail claims in Colorado in Q2 2025.",
      claims_kept: [{ text: "We paid $4,108,453 on hail claims in Colorado in Q2 2025.", citations: [] }],
      claims_cut: 0,
      could_not_confirm: [],
      citations: [],
    },
  ],
  [
    "done",
    {
      request_id: "5b0c1f3e-2a47-4c1d-9e8f-0a1b2c3d4e5f",
      total_ms: 61.5,
      route: "quantitative",
      outcome: "answer",
      claim_ids: [],
      doc_ids: [],
    },
  ],
];

const FLOOD = {
  chunk_id: "ho-2025#7-3",
  doc_id: "ho-2025",
  anchor: "ho-2025#flood",
  title: "Homeowners Policy, Form HO-2025",
  header: "HO-2025 policy: Homeowners Policy | Section I Exclusions > Flood | edition HO-2025",
  body: "We do not cover loss caused by flood, surface water, waves or overflow of any body of water.",
  score: 0.8734,
  edition: "HO-2025",
};

export const policy: [string, unknown][] = [
  ["stage", { name: "route", ms: 3 }],
  [
    "evidence",
    {
      kind: "chunks",
      payload: [
        FLOOD,
        { ...FLOOD, chunk_id: "ho-2025#2-1", header: "HO-2025 policy | Definitions > Flood", score: 0.51 },
      ],
    },
  ],
  [
    "answer",
    {
      text: "Flood damage is excluded under HO-2025.\n- Surface water is excluded too.",
      claims_kept: [{ text: "Flood damage is excluded under HO-2025.", citations: ["ho-2025#7-3"] }],
      claims_cut: 1,
      could_not_confirm: [
        "One statement cited a source that wasn't among the documents found for you, so I left it out.",
      ],
      citations: [FLOOD],
    },
  ],
  [
    "done",
    { request_id: "7c0c1f3e-2a47-4c1d-9e8f-0a1b2c3d4e5f", total_ms: 812, route: "qualitative", outcome: "answer" },
  ],
];

export const split = {
  delta_total: 1200000,
  count_effect: 800000,
  mean_effect: 400000,
  groups: [
    { group: "hail", delta_total: 1168800, count_effect: 790000, mean_effect: 378800, share: 0.974 },
    { group: "wind", delta_total: 31200, count_effect: 10000, mean_effect: 21200, share: 0.026 },
  ],
};

export const why: [string, unknown][] = [
  ["stage", { name: "route", ms: 5 }],
  ["evidence", { kind: "sql", payload: { sql: "SELECT 1", params: [] } }],
  ["evidence", { kind: "sql", payload: { sql: "SELECT 2", params: [] } }],
  [
    "evidence",
    {
      kind: "rows",
      payload: [
        { period: "2025Q1", groups: { peril: "hail" }, value: 100 },
        { period: "2025Q1", groups: { peril: "wind" }, value: 50 },
      ],
    },
  ],
  ["evidence", { kind: "sandbox", payload: [split] }],
  ["evidence", { kind: "chunks", payload: [] }],
  [
    "answer",
    {
      text: "Hail claims account for 97.4% of the rise.",
      claims_kept: [{ text: "Hail claims account for 97.4% of the rise.", citations: [] }],
      claims_cut: 0,
      citations: [],
    },
  ],
  ["done", { request_id: "9d0c1f3e-2a47-4c1d-9e8f-0a1b2c3d4e5f", total_ms: 2400, route: "why", outcome: "answer" }],
];

/** A turn built from these events the way the page builds one, finished. */
export function turnFrom(events: [string, unknown][], id = 1, question = "How much did we pay?"): Turn {
  return finish(
    events.reduce((turn, [type, data]) => applyEvent(turn, type, data), newTurn(id, question, 0)),
    100,
  );
}

/** A response body that streams these events as server-sent events, split into small chunks. */
export function streamOf(events: [string, unknown][], chunk = 7): ReadableStream<Uint8Array> {
  const text = events
    .map(([type, data]) => `event: ${type}\r\ndata: ${JSON.stringify({ ...(data as object), type })}\r\n\r\n`)
    .join("");
  const bytes = new TextEncoder().encode(text);
  let at = 0;
  return new ReadableStream({
    pull(controller) {
      if (at >= bytes.length) {
        controller.close();
        return;
      }
      controller.enqueue(bytes.slice(at, at + chunk));
      at += chunk;
    },
  });
}
