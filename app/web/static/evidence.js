// Builds the collapsible evidence panel under an answer. Everything from the server goes in as text, never HTML.

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

export const fmtMs = (ms) => {
  const n = Number(ms) || 0;
  return n >= 1000 ? `${(n / 1000).toFixed(2)} s` : `${Math.round(n)} ms`;
};

// A passage header reads "title | section path | edition X", and the last step of the path names the section.
export function sourceLabel(source) {
  if (typeof source !== "object" || source === null) return String(source);
  const path = String(source.header ?? "").split(" | ").find((part) => part && part !== source.title && !part.startsWith("edition "));
  const section = (path ?? source.section ?? "").split(" > ").pop();
  return [source.title ?? source.doc_id ?? "Source", section, source.edition && `edition ${source.edition}`].filter(Boolean).join(" · ");
}

const KIND_LABELS = { sql: "SQL", rows: "rows", chunks: "passages", scan: "scan", sandbox: "sandbox" };
const asList = (value) => (Array.isArray(value) ? value : value == null ? [] : [value]);
const words = (name) => String(name).replaceAll("_", " ");

function section(title) {
  const box = el("section");
  box.append(el("h3", null, title));
  return box;
}

function code(text, className = "code") {
  const pre = el("pre", className);
  pre.append(el("code", null, text));
  return pre;
}

function cellText(value) {
  if (value == null) return "";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

const numeric = (v) => typeof v === "number" || (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v)));

function sqlBlock(payload, me) {
  const text = typeof payload === "string" ? payload : payload?.sql;
  if (!text) return null;
  const role = (typeof payload === "object" && payload?.role) || me.role;
  const box = section("SQL");
  box.append(el("p", "role-note", `Ran on ${me.name}'s own database login, ${role}, so row-level security applied.`));
  box.append(code(text, "sql"));
  return box;
}

function rowsTable(payload, limit = 20) {
  const rows = asList(Array.isArray(payload) ? payload : payload?.rows);
  const box = section("Rows");
  if (!rows.length) {
    box.append(el("p", "legend", "No rows came back."));
    return box;
  }
  const columns = payload?.columns ?? (Array.isArray(rows[0]) ? rows[0].map((_, i) => `column ${i + 1}`) : Object.keys(rows[0]));
  const table = el("table", "rows");
  const head = table.createTHead().insertRow();
  for (const name of columns) {
    const th = el("th", null, name);
    th.scope = "col";
    head.append(th);
  }
  const body = table.createTBody();
  for (const row of rows.slice(0, limit)) {
    const tr = body.insertRow();
    for (const value of Array.isArray(row) ? row : columns.map((name) => row[name])) {
      const td = tr.insertCell();
      td.textContent = cellText(value);
      if (numeric(value)) td.className = "num";
    }
  }
  const wrap = el("div", "table-wrap");
  wrap.append(table);
  box.append(wrap);
  const total = payload?.row_count ?? rows.length;
  if (total > limit || payload?.truncated) {
    box.append(el("p", "legend", `Showing ${Math.min(limit, rows.length)} of ${total}${payload?.truncated ? " or more" : ""} rows.`));
  }
  return box;
}

function chunkList(payload) {
  const chunks = asList(Array.isArray(payload) ? payload : payload?.chunks ?? payload?.hits);
  if (!chunks.length) return null;
  const box = section("Retrieved passages");
  const list = el("ul", "chunks");
  for (const chunk of chunks) {
    const item = el("li");
    item.append(el("span", "where", sourceLabel(chunk)));
    const score = chunk.score ?? chunk.rerank_score ?? chunk.similarity;
    if (typeof score === "number") item.append(el("span", "score", `score ${score.toFixed(3)}`));
    const body = chunk.body ?? chunk.text ?? chunk.snippet;
    if (body) item.append(el("p", "body", body));
    list.append(item);
  }
  box.append(list);
  return box;
}

// Scans are rendered at about twice screen resolution, so a crop is drawn at half size to match the page text.
const PAD = 12;
const SCALE = 0.5;
const images = new Map();

function loadScan(docId) {
  if (!images.has(docId)) {
    images.set(docId, new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = reject;
      img.src = `/evidence/scan/${encodeURIComponent(docId)}`;
    }));
  }
  return images.get(docId);
}

function scanFigure(field) {
  if (!field?.doc_id) return null;
  const figure = el("figure", "scan");
  const confidence = typeof field.confidence === "number" ? (field.confidence <= 1 ? field.confidence * 100 : field.confidence) : null;
  const caption = [
    field.field && `${words(field.field)}: ${field.value ?? "not read"}`,
    confidence !== null && `OCR confidence ${Math.round(confidence)}%`,
    field.flagged && (field.flag_reason || "flagged for review"),
  ].filter(Boolean).join(", ") || field.title || field.doc_id;
  figure.append(el("figcaption", null, caption));
  loadScan(field.doc_id).then((img) => {
    const [x, y, w, h] = Array.isArray(field.bbox) && field.bbox.length === 4 ? field.bbox : [0, 0, img.naturalWidth, img.naturalHeight];
    const sx = Math.max(0, x - PAD);
    const sy = Math.max(0, y - PAD);
    const sw = Math.min(img.naturalWidth, x + w + PAD) - sx;
    const sh = Math.min(img.naturalHeight, y + h + PAD) - sy;
    const canvas = document.createElement("canvas");
    canvas.width = sw;
    canvas.height = sh;
    canvas.style.width = `${Math.round(sw * SCALE)}px`;
    canvas.getContext("2d").drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `Part of scan ${field.doc_id}. ${caption}`);
    figure.prepend(canvas);
  }, () => figure.prepend(el("p", "legend", "The scan image is not available.")));
  return figure;
}

function sandboxBlock(payload) {
  const runs = asList(payload);
  if (!runs.length) return null;
  const box = section("Sandbox");
  for (const run of runs) {
    if (typeof run !== "object" || run === null) {
      box.append(code(String(run)));
      continue;
    }
    for (const [key, value] of Object.entries(run)) {
      if (value == null || value === "") continue;
      box.append(el("p", "legend", words(key)), code(typeof value === "string" ? value : JSON.stringify(value, null, 2)));
    }
  }
  return box;
}

function verifierBlock(verifier) {
  if (!verifier) return null;
  const kept = asList(verifier.kept);
  const reasons = asList(verifier.reasons);
  const cut = typeof verifier.cut === "number" ? verifier.cut : asList(verifier.cut).length;
  const box = section("Verifier");
  box.append(el("p", "legend", `${kept.length} ${kept.length === 1 ? "claim" : "claims"} kept, ${cut} cut.`));
  const list = el("ul", "claims");
  for (const claim of kept) {
    const item = el("li");
    item.append(el("span", "tag", "Kept"), typeof claim === "string" ? claim : claim?.text ?? "");
    list.append(item);
  }
  // A cut claim is shown by its reason only: its figure was wrong, or its source is not this user's to read.
  for (const reason of reasons) {
    const item = el("li", "cut");
    item.append(el("span", "tag", "Cut"), String(reason));
    list.append(item);
  }
  if (list.childElementCount) box.append(list);
  return box;
}

function timingBar(stages) {
  if (!stages.length) return null;
  const total = stages.reduce((sum, stage) => sum + (Number(stage.ms) || 0), 0);
  const box = section("Stage timings");
  const bar = el("div", "timings");
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", stages.map((stage) => `${words(stage.name)} ${fmtMs(stage.ms)}`).join(", "));
  for (const stage of stages) {
    const part = el("span");
    part.style.flexGrow = String(Math.max(Number(stage.ms) || 0, total / 100));
    part.title = `${words(stage.name)}: ${fmtMs(stage.ms)}`;
    bar.append(part);
  }
  const legend = stages.map((stage) => `${words(stage.name)} ${fmtMs(stage.ms)}`).join(" · ");
  box.append(bar, el("p", "legend", `${legend} · total ${fmtMs(total)}`));
  return box;
}

function scanSection(payload) {
  const fields = asList(Array.isArray(payload) ? payload : payload?.fields ?? payload).slice(0, 6);
  const box = section("Scan");
  for (const field of fields) {
    const figure = scanFigure(field);
    if (figure) box.append(figure);
  }
  return box.childElementCount > 1 ? box : null;
}

export function evidencePanel({ evidence, stages, verifier }, me) {
  const builders = { sql: (p) => sqlBlock(p, me), rows: rowsTable, chunks: chunkList, scan: scanSection, sandbox: sandboxBlock };
  const other = (item) => {
    const box = section(words(item.kind));
    box.append(code(cellText(item.payload)));
    return box;
  };
  const blocks = evidence.map((item) => (builders[item.kind] ? builders[item.kind](item.payload) : other(item)));
  blocks.push(verifierBlock(verifier), timingBar(stages));
  const shown = blocks.filter(Boolean);
  if (!shown.length) return null;
  const kinds = [...new Set(evidence.map((item) => KIND_LABELS[item.kind] ?? words(item.kind)))];
  if (verifier) kinds.push("verifier");
  if (stages.length) kinds.push("timings");
  const details = el("details", "evidence");
  details.append(el("summary", null, `Evidence: ${kinds.join(", ")}`), ...shown);
  return details;
}
