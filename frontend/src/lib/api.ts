import type { SessionView } from "@/lib/types";

/** A response the API refused, with the plain sentence it gave, such as a 401's or a 429's. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function refusal(response: Response): Promise<ApiError> {
  const text = (await response.text()).trim();
  return new ApiError(response.status, text || `The server answered ${response.status}.`);
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" }, signal: signal ?? null });
  if (!response.ok) throw await refusal(response);
  return (await response.json()) as T;
}

async function post(path: string, body: unknown, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  return fetch(path, { ...init, method: "POST", headers, body: JSON.stringify(body) });
}

export function getSession(signal?: AbortSignal): Promise<SessionView> {
  return getJson<SessionView>("/api/session", signal);
}

export function getDashboard<T>(source: string | null, signal?: AbortSignal): Promise<T> {
  return getJson<T>(source ? `/api/dashboard?source=${encodeURIComponent(source)}` : "/api/dashboard", signal);
}

/** Starts a new session as another demo user. Behind the sign-in proxy the route is a 404. */
export async function switchUser(user: string): Promise<boolean> {
  try {
    return (await post("/session", { user })).ok;
  } catch {
    return false;
  }
}

export async function sendFeedback(requestId: string, rating: 1 | -1, note?: string): Promise<boolean> {
  try {
    return (await post("/feedback", { request_id: requestId, rating, note: note ?? null })).ok;
  } catch {
    return false;
  }
}

/** Asks a question. The answer streams back as server-sent events on the response. */
export function askQuestion(question: string, signal: AbortSignal): Promise<Response> {
  return post("/ask", { q: question }, { signal, headers: { Accept: "text/event-stream" } });
}
