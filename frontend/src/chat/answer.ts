import type { Claim } from "@/lib/types";

// Markers are carried through the answer's text as control characters, so a marker can't be confused with anything
// the answer itself says, then split out when the text is laid out.
const OPEN = "\u0001";
const CLOSE = "\u0002";

/** A run of the answer's words, or a numbered marker pointing at one of its sources. */
export type Piece = string | number;

export type Paragraph = { kind: "p"; pieces: Piece[] } | { kind: "ul"; items: Piece[][] };

/**
 * Puts a numbered marker after each kept claim, pointing at the sources that claim cites. A marker the server wrote
 * into the text as [n] is kept as a marker too. The words are never changed.
 */
export function withMarkers(text: string, claims: Claim[], numbers: Map<string, number>): string {
  let out = "";
  let cursor = 0;
  for (const claim of claims) {
    const refs = [
      ...new Set((claim.citations ?? []).map((id) => numbers.get(id)).filter((n): n is number => n !== undefined)),
    ];
    const at = refs.length && claim.text ? text.indexOf(claim.text, cursor) : -1;
    if (at < 0 || !claim.text) continue;
    out += text.slice(cursor, at + claim.text.length) + refs.map((n) => `${OPEN}${n}${CLOSE}`).join("");
    cursor = at + claim.text.length;
  }
  return (out + text.slice(cursor)).replace(/\[(\d+)\]/g, `${OPEN}$1${CLOSE}`);
}

export function pieces(line: string): Piece[] {
  return line
    .split(new RegExp(`${OPEN}(\\d+)${CLOSE}`))
    .flatMap((part, index): Piece[] => (index % 2 === 0 ? (part ? [part] : []) : [Number(part)]));
}

/** The answer laid out one paragraph per line the server sent, with lines that start "- " gathered into lists. */
export function paragraphs(marked: string): Paragraph[] {
  const out: Paragraph[] = [];
  let bullets: Piece[][] | null = null;
  for (const line of marked.split("\n")) {
    if (!line.trim()) {
      bullets = null;
      continue;
    }
    if (line.startsWith("- ")) {
      if (!bullets) {
        bullets = [];
        out.push({ kind: "ul", items: bullets });
      }
      bullets.push(pieces(line.slice(2)));
    } else {
      bullets = null;
      out.push({ kind: "p", pieces: pieces(line) });
    }
  }
  return out;
}
