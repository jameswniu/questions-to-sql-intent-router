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

// A passage header reads "document | section path | edition X | Region region", each part after the first only when
// the document has one. The first part names the document, as "REF kind: title" when it has a reference number, and
// the last step of the path names the section, so two passages from one document are told apart by their sections.
export function sourceLabel(source) {
  if (typeof source !== "object" || source === null) return String(source);
  const [heading, ...parts] = String(source.header ?? "").split(" | ");
  const name = heading || source.title || source.doc_id || "Source";
  const tags = [source.edition && `edition ${source.edition}`, source.region && `${source.region} region`];
  const path = parts.find((part) => part && !tags.includes(part)) ?? source.section ?? "";
  const section = String(path).split(" > ").pop();
  const edition = source.edition && !name.includes(source.edition) && `edition ${source.edition}`;
  return [name, section, edition].filter(Boolean).join(" · ");
}

// One entry per chunk, however many sentences cite it or however often it comes back.
export const sourceKey = (source, index) => source?.chunk_id ?? source?.doc_id ?? `#${index}`;
export function uniqueSources(sources) {
  const seen = new Map();
  sources.forEach((source, index) => {
    const key = sourceKey(source, index);
    if (!seen.has(key)) seen.set(key, source);
  });
  return [...seen.values()];
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

// psycopg marks each bound value with %s and writes a literal percent sign as %%.
const placeholders = (sql) => sql.replaceAll("%%", "").split("%s").length - 1;

function paramText(value) {
  if (value === null || value === undefined) return "NULL";
  if (Array.isArray(value)) return `[${value.map(paramText).join(", ")}]`;
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function legendWith(...parts) {
  const note = el("p", "legend");
  note.append(...parts);
  return note;
}

// What ran was parameterized, so the values stay out of the SQL text and are listed under it, in placeholder order.
function boundParams(sql, params) {
  const values = Array.isArray(params) ? params : [];
  const mark = () => el("code", null, "%s");
  if (!values.length) {
    const slots = placeholders(sql);
    const rest = `${slots === 1 ? "placeholder was" : "placeholders were"} bound as parameters; their values aren't shown here.`;
    return slots ? [legendWith(`The ${slots} `, mark(), ` ${rest}`)] : [];
  }
  const list = el("ol", "params");
  for (const value of values) {
    const item = el("li");
    item.append(el("code", null, paramText(value)));
    list.append(item);
  }
  return [legendWith("Parameters bound to the ", mark(), " placeholders, in order:"), list];
}

function sqlBlock(payload, me) {
  const text = typeof payload === "string" ? payload : payload?.sql;
  if (!text) return null;
  const role = (typeof payload === "object" && payload?.role) || me.role;
  const box = section("SQL");
  box.append(el("p", "role-note", `Ran on ${me.name}'s own database login, ${role}, so row-level security applied.`));
  box.append(code(text, "sql"), ...boundParams(text, typeof payload === "object" ? payload?.params : null));
  return box;
}

// Rows from different queries carry different keys, so the columns are every key any row has, in first-seen order.
function columnsOf(rows) {
  if (rows.some(Array.isArray)) {
    const width = rows.reduce((most, row) => Math.max(most, Array.isArray(row) ? row.length : 0), 0);
    return Array.from({ length: width }, (_, i) => `column ${i + 1}`);
  }
  const seen = new Set();
  for (const row of rows) {
    if (row && typeof row === "object") for (const key of Object.keys(row)) seen.add(key);
  }
  return [...seen];
}

function rowsTable(payload, limit = 20) {
  const rows = asList(Array.isArray(payload) ? payload : payload?.rows);
  const box = section("Rows");
  if (!rows.length) {
    box.append(el("p", "legend", "No rows came back."));
    return box;
  }
  const columns = payload?.columns ?? columnsOf(rows);
  const table = el("table", "rows");
  const head = table.createTHead().insertRow();
  for (const name of columns) {
    const th = el("th", null, name);
    th.scope = "col";
    head.append(th);
  }
  const body = table.createTBody();
  const folded = [];
  rows.forEach((row, index) => {
    const tr = body.insertRow();
    if (index >= limit) {
      tr.hidden = true;
      folded.push(tr);
    }
    // A row without one of the columns leaves that cell blank.
    for (const value of columns.map((name, i) => (Array.isArray(row) ? row[i] : row?.[name]))) {
      const td = tr.insertCell();
      td.textContent = cellText(value);
      if (numeric(value)) td.className = "num";
    }
  });
  const wrap = el("div", "table-wrap");
  wrap.append(table);
  box.append(wrap);
  const total = payload?.row_count ?? rows.length;
  if (total > limit || payload?.truncated) {
    const count = (shown) => `Showing ${shown} of ${total}${payload?.truncated ? " or more" : ""} rows.`;
    const legend = el("p", "legend", count(Math.min(limit, rows.length)));
    if (folded.length) {
      // The rows past the first few are often another query's, and hold the only values in some columns.
      const more = el("button", "more", "Show all");
      more.type = "button";
      more.addEventListener("click", () => {
        for (const tr of folded) tr.hidden = false;
        legend.textContent = count(rows.length);
      });
      legend.append(" ", more);
    }
    box.append(legend);
  }
  return box;
}

function chunkList(payload) {
  const chunks = uniqueSources(asList(Array.isArray(payload) ? payload : payload?.chunks ?? payload?.hits));
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

// The decompose template's output: a change in a total, split into count and mean effects, overall and per group.
const isSplit = (run) => typeof run === "object" && run !== null && ["delta_total", "count_effect", "mean_effect"].every((key) => key in run);
const SPLIT_COLUMNS = ["Group", "Change", "Count effect", "Mean effect", "Share"];
const SPLIT_NOTE =
  "Split in the sandbox: the count effect is the part of a change from the number of claims, the mean effect the part from the average per claim, and the share each group's part of the whole change.";
const percent = new Intl.NumberFormat("en-US", { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1 });

// The split names its groups but not what they are, and the rows show it: the column holding every one of them.
function dimensionOf(run, rows) {
  const keys = (Array.isArray(run.groups) ? run.groups : []).map((group) => String(group.group));
  if (!keys.length) return null;
  const values = new Map();
  const add = (name, value) => {
    if (value === null || typeof value === "object") return;
    if (!values.has(name)) values.set(name, new Set());
    values.get(name).add(String(value));
  };
  for (const row of rows) {
    if (!row || typeof row !== "object" || Array.isArray(row)) continue;
    for (const [name, value] of Object.entries(row)) {
      // The aggregate views give a row's groups as one object, such as {"peril": "hail"}.
      if (value && typeof value === "object" && !Array.isArray(value)) Object.entries(value).forEach(([inner, v]) => add(inner, v));
      else add(name, value);
    }
  }
  for (const [name, seen] of values) if (name !== "period" && keys.every((key) => seen.has(key))) return name;
  return null;
}

function splitTable(run, dimension) {
  const groups = Array.isArray(run.groups) ? run.groups : [];
  const whole = { ...run, group: "Total", share: Number(run.delta_total) ? 1 : null };
  const figures = [...groups, whole].flatMap((line) => [line.delta_total, line.count_effect, line.mean_effect]).filter((v) => v != null);
  const places = figures.every((v) => Number.isInteger(Number(v))) ? 0 : 2;
  const amount = new Intl.NumberFormat("en-US", { minimumFractionDigits: places, maximumFractionDigits: places, signDisplay: "exceptZero" });
  const table = el("table", "split");
  if (dimension) table.createCaption().textContent = `By ${words(dimension)}`;
  const head = table.createTHead().insertRow();
  for (const name of SPLIT_COLUMNS) {
    const th = el("th", null, name);
    th.scope = "col";
    head.append(th);
  }
  const addLine = (part, line) => {
    const tr = part.insertRow();
    const name = el("th", null, line.group ?? "");
    name.scope = "row";
    tr.append(name);
    for (const [value, format] of [[line.delta_total, amount], [line.count_effect, amount], [line.mean_effect, amount], [line.share, percent]]) {
      const td = tr.insertCell();
      td.className = "num";
      td.textContent = value == null || !Number.isFinite(Number(value)) ? "" : format.format(Number(value));
    }
  };
  const body = table.createTBody();
  for (const group of groups) addLine(body, group);
  addLine(groups.length ? table.createTFoot() : body, whole);
  const wrap = el("div", "table-wrap");
  wrap.append(table);
  return wrap;
}

function sandboxBlock(payload, rows) {
  const runs = asList(payload);
  if (!runs.length) return null;
  const splits = runs.filter(isSplit);
  const box = section(splits.length === runs.length ? "Driver split" : "Sandbox");
  if (splits.length) box.append(el("p", "legend", SPLIT_NOTE));
  for (const run of runs) {
    if (isSplit(run)) {
      box.append(splitTable(run, dimensionOf(run, rows)));
      continue;
    }
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

const kindLabel = (item) =>
  item.kind === "sandbox" && asList(item.payload).some(isSplit) ? "driver split" : KIND_LABELS[item.kind] ?? words(item.kind);

export function evidencePanel({ evidence, stages, verifier }, me) {
  const rows = evidence.filter((item) => item.kind === "rows").flatMap((item) => asList(Array.isArray(item.payload) ? item.payload : item.payload?.rows));
  const builders = { sql: (p) => sqlBlock(p, me), rows: rowsTable, chunks: chunkList, scan: scanSection, sandbox: (p) => sandboxBlock(p, rows) };
  const other = (item) => {
    const box = section(words(item.kind));
    box.append(code(cellText(item.payload)));
    return box;
  };
  // A why answer is about its driver split, so the split leads, ahead of the SQL and the rows it was worked out from.
  const ordered = [...evidence.filter((item) => item.kind === "sandbox"), ...evidence.filter((item) => item.kind !== "sandbox")];
  const built = ordered.map((item) => [item, builders[item.kind] ? builders[item.kind](item.payload) : other(item)]);
  const blocks = built.map(([, block]) => block);
  blocks.push(verifierBlock(verifier), timingBar(stages));
  const shown = blocks.filter(Boolean);
  if (!shown.length) return null;
  const kinds = [...new Set(built.filter(([, block]) => block).map(([item]) => kindLabel(item)))];
  if (verifier) kinds.push("verifier");
  if (stages.length) kinds.push("timings");
  const details = el("details", "evidence");
  details.append(el("summary", null, `Evidence: ${kinds.join(", ")}`), ...shown);
  return details;
}
