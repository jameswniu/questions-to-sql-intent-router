import type { Verifier } from "@/chat/turn";
import { counted, fmtMs } from "@/lib/format";
import { uniqueSources } from "@/lib/sources";
import { asText } from "@/lib/text";
import type { EvidenceEvent, StageEvent } from "@/lib/types";

// What the evidence panel is built from, worked out from the stream's evidence events. Everything from the server is
// shown as text; nothing here changes what it says.

export const asList = (value: unknown): unknown[] => (Array.isArray(value) ? value : value == null ? [] : [value]);

export const words = (name: unknown): string => String(name).replaceAll("_", " ");

export const record = (value: unknown): Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};

export const cellText = asText;

export const numeric = (value: unknown): boolean =>
  typeof value === "number" || (typeof value === "string" && value.trim() !== "" && !Number.isNaN(Number(value)));

/** How many values a statement binds. psycopg marks each with %s and writes a literal percent sign as %%. */
export const placeholders = (sql: string): number => sql.replaceAll("%%", "").split("%s").length - 1;

export function paramText(value: unknown): string {
  if (value === null || value === undefined) return "NULL";
  if (Array.isArray(value)) return `[${value.map(paramText).join(", ")}]`;
  return asText(value);
}

/** Rows from different queries carry different keys, so the columns are every key any row has, in first-seen order. */
export function columnsOf(rows: unknown[]): string[] {
  if (rows.some(Array.isArray)) {
    const width = rows.reduce<number>((most, row) => Math.max(most, Array.isArray(row) ? row.length : 0), 0);
    return Array.from({ length: width }, (_, i) => `column ${i + 1}`);
  }
  const seen = new Set<string>();
  for (const row of rows) for (const key of Object.keys(record(row))) seen.add(key);
  return [...seen];
}

export function rowsOf(payload: unknown): unknown[] {
  return asList(Array.isArray(payload) ? payload : record(payload).rows);
}

// The decompose template's output: a change in a total, split into count and mean effects, overall and per group.
export interface SplitLine {
  group?: unknown;
  delta_total?: unknown;
  count_effect?: unknown;
  mean_effect?: unknown;
  share?: unknown;
}

export interface SplitRun extends SplitLine {
  groups?: SplitLine[];
}

export const isSplit = (run: unknown): run is SplitRun => {
  const found = record(run);
  return ["delta_total", "count_effect", "mean_effect"].every((key) => key in found);
};

export const SPLIT_COLUMNS = ["Group", "Change", "Count effect", "Mean effect", "Share"];
export const SPLIT_NOTE =
  "Split in the sandbox: the count effect is the part of a change from the number of claims, the mean effect the part from the average per claim, and the share each group's part of the whole change.";

/** The split names its groups but not what they are, and the rows show it: the column holding every one of them. */
export function dimensionOf(run: SplitRun, rows: unknown[]): string | null {
  const keys = (Array.isArray(run.groups) ? run.groups : []).map((group) => cellText(group.group));
  if (!keys.length) return null;
  const values = new Map<string, Set<string>>();
  const add = (name: string, value: unknown) => {
    if (value === null || typeof value === "object") return;
    if (!values.has(name)) values.set(name, new Set());
    values.get(name)?.add(asText(value));
  };
  for (const row of rows) {
    if (!row || typeof row !== "object" || Array.isArray(row)) continue;
    for (const [name, value] of Object.entries(row as Record<string, unknown>)) {
      // The aggregate views give a row's groups as one object, such as {"peril": "hail"}.
      if (value && typeof value === "object" && !Array.isArray(value)) {
        for (const [inner, v] of Object.entries(value)) add(inner, v);
      } else add(name, value);
    }
  }
  for (const [name, seen] of values) if (name !== "period" && keys.every((key) => seen.has(key))) return name;
  return null;
}

// Evidence tabs, in the order a reader wants them: what the answer rests on first, how it was checked last.
export type TabKind = "sandbox" | "scan" | "sql" | "rows" | "chunks" | "other" | "checks" | "timings";

export interface EvidenceTab {
  id: string;
  kind: TabKind;
  /** The tab's name. */
  label: string;
  /** The panel's heading, which names what it holds in full. */
  heading: string;
  /** A count on the tab, such as the number of rows. */
  count: string | null;
  /** The same thing said in a few words, for the panel's summary line. */
  chip: string;
  items: EvidenceEvent[];
}

const ORDER: TabKind[] = ["sandbox", "scan", "sql", "rows", "chunks", "other", "checks", "timings"];
const SCAN_FIELDS_SHOWN = 6;

/** A field's caption: what was read, how sure the OCR was, and why it was flagged, if it was. */
export function scanCaption(field: Record<string, unknown>): string {
  const confidence =
    typeof field.confidence === "number" ? (field.confidence <= 1 ? field.confidence * 100 : field.confidence) : null;
  const value = field.value ?? "not read";
  const parts = [
    field.field ? `${words(field.field)}: ${cellText(value)}` : "",
    confidence !== null ? `OCR confidence ${Math.round(confidence)}%` : "",
    field.flagged ? cellText(field.flag_reason) || "flagged for review" : "",
  ];
  return parts.filter(Boolean).join(", ") || cellText(field.title) || cellText(field.doc_id);
}

export function scanFields(payload: unknown): Record<string, unknown>[] {
  const list = asList(Array.isArray(payload) ? payload : (record(payload).fields ?? payload));
  return list
    .slice(0, SCAN_FIELDS_SHOWN)
    .map(record)
    .filter((field) => Boolean(field.doc_id));
}

export function passages(payload: unknown): Record<string, unknown>[] {
  const found = record(payload);
  return uniqueSources(asList(Array.isArray(payload) ? payload : (found.chunks ?? found.hits))).map(record);
}

function sqlText(payload: unknown): string {
  return typeof payload === "string" ? payload : cellText(record(payload).sql);
}

/** The tabs an answer's evidence fills, in reading order. A kind with nothing to show gets no tab. */
export function evidenceTabs(
  evidence: EvidenceEvent[],
  stages: StageEvent[],
  verifier: Verifier | null,
): EvidenceTab[] {
  const tabs = new Map<string, EvidenceTab>();
  const add = (id: string, kind: TabKind, label: string, heading: string, item: EvidenceEvent) => {
    const tab = tabs.get(id) ?? { id, kind, label, heading, count: null, chip: label, items: [] };
    tab.items.push(item);
    tabs.set(id, tab);
  };
  for (const item of evidence) {
    if (item.kind === "sql" && sqlText(item.payload)) add("sql", "sql", "SQL", "SQL", item);
    else if (item.kind === "rows") add("rows", "rows", "Rows", "Rows", item);
    else if (item.kind === "chunks" && passages(item.payload).length) {
      add("chunks", "chunks", "Sources", "Retrieved passages", item);
    } else if (item.kind === "scan" && scanFields(item.payload).length) add("scan", "scan", "Scan", "Scan", item);
    else if (item.kind === "sandbox" && asList(item.payload).length) {
      const split = asList(item.payload).every(isSplit);
      add("sandbox", "sandbox", split ? "Driver split" : "Sandbox", split ? "Driver split" : "Sandbox", item);
    } else if (!["sql", "rows", "chunks", "scan", "sandbox"].includes(item.kind)) {
      add(`other-${item.kind}`, "other", words(item.kind), words(item.kind), item);
    }
  }
  for (const tab of tabs.values()) {
    if (tab.kind === "sql" && tab.items.length > 1) {
      tab.count = String(tab.items.length);
      tab.chip = counted(tab.items.length, "query", "queries");
    } else if (tab.kind === "rows") {
      const total = tab.items.reduce((sum, item) => {
        const count = Number(record(item.payload).row_count);
        return sum + (Number.isFinite(count) && count > 0 ? count : rowsOf(item.payload).length);
      }, 0);
      tab.count = total.toLocaleString("en-US");
      tab.chip = total ? counted(total, "row") : "No rows";
    } else if (tab.kind === "chunks") {
      const total = tab.items.reduce((sum, item) => sum + passages(item.payload).length, 0);
      tab.count = String(total);
      tab.chip = counted(total, "passage");
    } else if (tab.kind === "scan") {
      const total = tab.items.reduce((sum, item) => sum + scanFields(item.payload).length, 0);
      tab.count = String(total);
      tab.chip = counted(total, "scan field");
    }
  }
  const list = [...tabs.values()];
  if (verifier) {
    const kept = verifier.kept.length;
    list.push({
      id: "checks",
      kind: "checks",
      label: "Checks",
      heading: "Verifier",
      count: String(kept + verifier.cut),
      chip: `${kept} kept, ${verifier.cut} cut`,
      items: [],
    });
  }
  if (stages.length) {
    const total = stages.reduce((sum, stage) => sum + (stage.ms || 0), 0);
    list.push({
      id: "timings",
      kind: "timings",
      label: "Timings",
      heading: "Stage timings",
      count: null,
      chip: fmtMs(total),
      items: [],
    });
  }
  return list.sort((a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind));
}
