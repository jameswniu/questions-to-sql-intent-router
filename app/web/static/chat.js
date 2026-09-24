import { el, evidencePanel, fmtMs, sourceLabel } from "./evidence.js";
import { feedback } from "./feedback.js";

const thread = document.getElementById("thread");
const form = document.getElementById("composer");
const box = document.getElementById("q");
const send = document.getElementById("send");
const switcher = document.getElementById("user");
const me = { name: document.body.dataset.name, role: document.body.dataset.role };

let inFlight = null;
let turns = 0;

// Only demo mode has a switcher; behind the sign-in proxy the page names the user instead.
switcher?.addEventListener("change", async () => {
  switcher.disabled = true;
  const res = await fetch("/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user: switcher.value }),
  }).catch(() => null);
  if (res?.ok) location.reload();
  else switcher.disabled = false;
});

box.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (inFlight) {
    inFlight.abort();
    return;
  }
  const question = box.value.trim();
  if (!question) return;
  box.value = "";
  ask(question);
});

document.addEventListener("click", (event) => {
  const option = event.target.closest("[data-fill]");
  if (option) {
    box.value = option.dataset.fill;
    box.focus();
  }
});

function setBusy(busy) {
  send.textContent = busy ? "Stop" : "Send";
  send.classList.toggle("stop", busy);
}

async function ask(question) {
  document.getElementById("intro")?.remove();
  const card = el("section", "answer");
  const progress = el("p", "progress", "Sent. Waiting for the first step.");
  progress.setAttribute("role", "status");
  card.append(progress);
  const turn = el("article", "turn");
  turn.append(el("p", "question", question), card);
  thread.append(turn);
  turn.scrollIntoView({ block: "start" });

  const state = { id: ++turns, card, progress, started: performance.now(), stages: [], evidence: [], verifier: null };
  inFlight = new AbortController();
  setBusy(true);
  try {
    const res = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ q: question }),
      signal: inFlight.signal,
    });
    if (!res.ok) {
      notice(state, "error", "Not sent", (await res.text()) || `The server answered ${res.status}.`);
      state.ended = true;
      return;
    }
    await readEvents(res.body, (type, data) => handle(state, type, data));
    if (!state.ended) notice(state, "error", "Stopped early", "The answer ended before it finished.");
  } catch (error) {
    if (error.name === "AbortError") notice(state, "outside", "Stopped", "You stopped this answer.");
    else notice(state, "error", "Connection lost", "The connection dropped before the answer finished.");
  } finally {
    inFlight = null;
    setBusy(false);
    finish(state);
  }
}

// Server-sent events over a POST response, since EventSource can only GET and the question stays out of URLs.
async function readEvents(stream, onEvent) {
  const reader = stream.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value.replace(/\r\n?/g, "\n");
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let type = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (!line || line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        const text = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (field === "event") type = text;
        else if (field === "data") data.push(text);
      }
      if (data.length) onEvent(type, JSON.parse(data.join("\n")));
    }
  }
}

function handle(state, type, data) {
  switch (type) {
    case "stage":
      state.stages.push(data);
      state.progress.textContent = `Working. ${state.stages.map((s) => `${s.name} ${fmtMs(s.ms)}`).join(", ")}`;
      break;
    case "evidence":
      state.evidence.push(data);
      break;
    case "answer":
      renderAnswer(state, data);
      break;
    case "refused":
      notice(state, "refused", "Not answered", data.message);
      break;
    case "clarify":
      renderClarify(state, data);
      break;
    case "out_of_data":
      renderOutside(state, data);
      break;
    case "done":
      state.done = data;
      state.ended = true;
      break;
    default:
      state.ended = true;
      notice(state, "error", "Something went wrong", data.message || "The answer could not be finished.");
  }
}

function notice(state, kind, label, message) {
  const box = el("div", `notice ${kind}`);
  box.append(el("span", "label", label), el("p", null, message ?? ""));
  state.card.append(box);
  return box;
}

// Puts a numbered marker after each kept claim, pointing at the sources that claim cites.
function withMarkers(text, claims, numbers) {
  let out = "";
  let cursor = 0;
  for (const claim of claims) {
    const refs = [...new Set((claim?.citations ?? []).map((id) => numbers.get(id)).filter(Boolean))];
    const at = refs.length && claim?.text ? text.indexOf(claim.text, cursor) : -1;
    if (at < 0) continue;
    out += text.slice(cursor, at + claim.text.length) + refs.map((n) => `\u0001${n}\u0002`).join("");
    cursor = at + claim.text.length;
  }
  return (out + text.slice(cursor)).replace(/\[(\d+)\]/g, "\u0001$1\u0002");
}

function appendMarked(node, line, turnId) {
  line.split(/\u0001(\d+)\u0002/).forEach((part, index) => {
    if (index % 2 === 0) {
      if (part) node.append(part);
      return;
    }
    const sup = el("sup", "cite");
    const link = el("a", null, part);
    link.href = `#cite-${turnId}-${part}`;
    link.setAttribute("aria-label", `Source ${part}`);
    sup.append(link);
    node.append(sup);
  });
}

function renderAnswer(state, data) {
  const citations = Array.isArray(data.citations) ? data.citations : [];
  const numbers = new Map(citations.map((c, i) => [c?.chunk_id ?? c?.doc_id ?? String(i), i + 1]));
  const marked = withMarkers(String(data.text ?? ""), Array.isArray(data.claims_kept) ? data.claims_kept : [], numbers);
  const body = el("div", "answer-text");
  let bullets = null;
  for (const line of marked.split("\n")) {
    if (!line.trim()) {
      bullets = null;
      continue;
    }
    if (line.startsWith("- ")) {
      bullets ??= body.appendChild(el("ul"));
      const item = el("li");
      appendMarked(item, line.slice(2), state.id);
      bullets.append(item);
    } else {
      bullets = null;
      const para = el("p");
      appendMarked(para, line, state.id);
      body.append(para);
    }
  }
  state.card.append(body);
  const reasons = Array.isArray(data.could_not_confirm) ? data.could_not_confirm : data.could_not_confirm ? ["Part of this could not be confirmed."] : [];
  for (const reason of reasons) state.card.append(el("p", "caveat", reason));
  if (citations.length) {
    const list = el("ol", "citations");
    citations.forEach((c, i) => {
      const item = el("li", null, sourceLabel(c));
      item.id = `cite-${state.id}-${i + 1}`;
      list.append(item);
    });
    state.card.append(list);
  }
  state.verifier = { kept: data.claims_kept, cut: data.claims_cut, reasons };
}

function renderClarify(state, data) {
  const box = notice(state, "clarify", "Needs one more detail", data.question ?? data.message);
  const options = Array.isArray(data.options) ? data.options : [];
  if (!options.length) return;
  const list = el("ul", "options");
  for (const option of options) {
    const text = typeof option === "string" ? option : option?.label ?? option?.value ?? "";
    const button = el("button", null, text);
    button.type = "button";
    button.dataset.fill = typeof option === "string" ? option : option?.value ?? text;
    const item = el("li");
    item.append(button);
    list.append(item);
  }
  box.append(list);
}

function renderOutside(state, data) {
  const box = notice(state, "outside", "Outside the data", data.message);
  const covered = data.covered;
  if (typeof covered === "string") box.append(el("p", "muted", covered));
  else if (covered?.start && covered?.end) box.append(el("p", "muted", `The data covers ${covered.start} to ${covered.end}.`));
}

function finish(state) {
  state.progress.remove();
  const panel = evidencePanel(state, me);
  if (panel) state.card.append(panel);
  if (!state.done) return;
  const footer = el("div", "footer");
  const total = state.done.total_ms ?? performance.now() - state.started;
  const request = el("span", null, "Request ");
  request.append(el("code", null, String(state.done.request_id).slice(0, 8)));
  footer.append(el("span", null, `Finished in ${fmtMs(total)}`), request, feedback(String(state.done.request_id)));
  state.card.append(footer);
}
