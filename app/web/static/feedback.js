import { el } from "./evidence.js";

const THUMB =
  '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round">' +
  '<path d="M1.5 7h3v7.5h-3z"/><path d="M4.5 7.5 7.6 2.3c.9-1 2.5-.3 2.3 1.1L9.4 6h3.8c1 0 1.7.9 1.5 1.9l-1 5.4' +
  'c-.2.8-.9 1.2-1.6 1.2H4.5"/></svg>';

async function postFeedback(requestId, rating, note) {
  try {
    const res = await fetch("/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: requestId, rating, note }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

export function feedback(requestId) {
  const wrap = el("div", "feedback");
  const status = el("span", null, "Useful?");
  status.setAttribute("role", "status");
  const buttons = [[1, "Useful", "thumb"], [-1, "Not useful", "thumb down"]].map(([rating, label, className]) => {
    const button = el("button", className);
    button.type = "button";
    button.title = label;
    button.setAttribute("aria-label", label);
    button.setAttribute("aria-pressed", "false");
    button.innerHTML = THUMB;
    button.addEventListener("click", async () => {
      buttons.forEach((b) => { b.disabled = true; });
      button.setAttribute("aria-pressed", "true");
      const saved = await postFeedback(requestId, rating);
      status.textContent = saved ? "Thanks, noted." : "Could not save that.";
      if (saved && rating < 0) wrap.after(noteForm(requestId, status));
    });
    return button;
  });
  wrap.append(status, ...buttons);
  return wrap;
}

function noteForm(requestId, status) {
  const note = el("form", "why-not");
  const input = el("input");
  input.type = "text";
  input.maxLength = 1000;
  input.placeholder = "What was wrong? (optional)";
  input.setAttribute("aria-label", "What was wrong");
  const button = el("button", "secondary", "Send");
  button.type = "submit";
  note.append(input, button);
  note.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    note.remove();
    status.textContent = (await postFeedback(requestId, -1, text)) ? "Thanks, noted." : "Could not save the note.";
  });
  return note;
}
