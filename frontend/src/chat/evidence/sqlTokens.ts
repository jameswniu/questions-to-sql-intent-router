// Splits SQL into tokens for highlighting. Joining the tokens gives back the statement exactly, so what is shown is
// what ran, character for character.

export type TokenKind = "keyword" | "function" | "string" | "number" | "placeholder" | "comment" | "plain";

const KEYWORDS = new Set(
  (
    "select from where and or not in is null as on join left right inner outer full cross group by order having " +
    "limit offset union all distinct case when then else end between like ilike any exists with asc desc filter " +
    "over partition interval true false cast using lateral values returning"
  ).split(" "),
);

// A line comment starts with two hyphens, written -{2} here.
const COMMENT = /^-{2}/;
const TOKEN = /-{2}[^\n]*|'(?:[^']|'')*'|%s|\d+(?:\.\d+)?\b|[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*|\s+|./g;

export function sqlTokens(sql: string): [TokenKind, string][] {
  const out: [TokenKind, string][] = [];
  for (const match of sql.matchAll(TOKEN)) {
    const token = match[0];
    const next = sql.slice(match.index + token.length).trimStart();
    let kind: TokenKind = "plain";
    if (COMMENT.test(token)) kind = "comment";
    else if (token.startsWith("'")) kind = "string";
    else if (token === "%s") kind = "placeholder";
    else if (/^\d/.test(token)) kind = "number";
    else if (KEYWORDS.has(token.toLowerCase())) kind = "keyword";
    else if (/^[A-Za-z_][\w$.]*$/.test(token) && next.startsWith("(")) kind = "function";
    const last = out.at(-1);
    if (last?.[0] === "plain" && kind === "plain") last[1] += token;
    else out.push([kind, token]);
  }
  return out;
}
