import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

type Reply = (request: { url: string; method: string; body: unknown }) => Response | Promise<Response>;

/** A stand-in for the API: each request goes to the handler for its method and path, and is recorded. */
export function fakeServer(routes: Record<string, Reply>) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const method = (init?.method ?? "GET").toUpperCase();
    const body: unknown = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    const call = { url, method, body };
    calls.push(call);
    const path = url.split("?")[0] ?? url;
    const reply = routes[`${method} ${path}`];
    if (!reply) return new Response("Not found", { status: 404 });
    return reply(call);
  });
  vi.stubGlobal("fetch", fetch);
  return calls;
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

export function renderWithQueries(element: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>{element}</TooltipProvider>
    </QueryClientProvider>,
  );
}
