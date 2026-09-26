import { CircleAlert } from "lucide-react";

import { ApiError } from "@/lib/api";

/** A request the page could not complete, with the server's own sentence when it gave one. */
export function ErrorPanel({ error, title = "This page could not load" }: { error: unknown; title?: string }) {
  const message =
    error instanceof ApiError
      ? error.message
      : "The server could not be reached. Check that the app is running, then reload the page.";
  return (
    <div
      role="alert"
      className="mx-auto mt-10 max-w-xl rounded-xl border border-danger-border bg-danger-soft p-5 text-foreground"
    >
      <p className="flex items-center gap-2 font-semibold text-danger">
        <CircleAlert className="size-5" aria-hidden="true" />
        {title}
      </p>
      <p className="mt-2 text-sm">{message}</p>
    </div>
  );
}

/** A quiet placeholder while the first data loads. It keeps the layout, so nothing jumps when the data arrives. */
export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden="true" className={`animate-pulse rounded-lg bg-surface-hover ${className ?? ""}`} />;
}
