export type PageName = "chat" | "dashboard";

/** Which page an address shows. The server serves the same index at / and /dashboard, and checks who may open each. */
export function pageFor(path: string): PageName {
  return path.replace(/\/+$/, "") === "/dashboard" ? "dashboard" : "chat";
}
