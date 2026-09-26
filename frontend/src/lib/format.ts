/** A duration in milliseconds the way the page writes it: 245 ms, or 1.20 s from a second up. */
export function fmtMs(ms: unknown): string {
  const n = Number(ms) || 0;
  return n >= 1000 ? `${(n / 1000).toFixed(2)} s` : `${Math.round(n)} ms`;
}

/** "1 row", "12 rows": a count and its noun, plural past one. */
export function counted(count: number, one: string, many = `${one}s`): string {
  return `${count.toLocaleString("en-US")} ${count === 1 ? one : many}`;
}

/** A user's initials, for the avatar beside their name. */
export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part.charAt(0).toUpperCase())
    .join("");
}
