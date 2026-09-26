import type { Source } from "@/lib/types";

// A passage header reads "document | section path | edition X | Region region", each part after the first only when
// the document has one. The first part names the document, as "REF kind: title" when it has a reference number, and
// the last step of the path names the section, so two passages from one document are told apart by their sections.
export function sourceLabel(source: unknown): string {
  if (typeof source !== "object" || source === null) return String(source);
  const hit = source as Source;
  const [heading, ...parts] = (hit.header ?? "").split(" | ");
  // The first of these that says anything names the document.
  const name = [heading, hit.title, hit.doc_id].find(Boolean) ?? "Source";
  const tags = [hit.edition && `edition ${hit.edition}`, hit.region && `${hit.region} region`];
  const path = parts.find((part) => part && !tags.includes(part)) ?? hit.section ?? "";
  const section = path.split(" > ").pop();
  const edition = hit.edition && !name.includes(hit.edition) && `edition ${hit.edition}`;
  return [name, section, edition].filter(Boolean).join(" · ");
}

/** One entry per chunk, however many sentences cite it or however often it comes back. */
export function sourceKey(source: unknown, index: number): string {
  const hit = (typeof source === "object" && source !== null ? source : {}) as Source;
  return hit.chunk_id ?? hit.doc_id ?? `#${index}`;
}

export function uniqueSources<T>(sources: T[]): T[] {
  const seen = new Map<string, T>();
  sources.forEach((source, index) => {
    const key = sourceKey(source, index);
    if (!seen.has(key)) seen.set(key, source);
  });
  return [...seen.values()];
}
