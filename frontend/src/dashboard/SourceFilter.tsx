import type { Filter } from "@/dashboard/types";
import { cn } from "@/lib/cn";

/**
 * Which requests the whole dashboard counts: all of them, or only those from the browser, the eval or the replay.
 * Each choice is a link, so it can be opened on its own, and a plain click swaps the view in place.
 */
export function SourceFilter({
  filters,
  current,
  onChoose,
}: {
  filters: Filter[];
  current: string | null;
  onChoose: (filter: Filter) => void;
}) {
  return (
    <nav
      className="filters inline-flex items-center gap-1 rounded-lg border border-border bg-surface-sunken p-1"
      aria-label="Requests from"
    >
      {filters.map((filter) => (
        <a
          key={filter.href}
          href={filter.href}
          aria-current={filter.source === current ? "page" : undefined}
          onClick={(event) => {
            if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            onChoose(filter);
          }}
          className={cn(
            "rounded-md px-3 py-1 text-sm font-medium text-muted transition-colors hover:text-foreground",
            "aria-[current=page]:bg-surface-raised aria-[current=page]:font-semibold aria-[current=page]:text-foreground aria-[current=page]:shadow-xs aria-[current=page]:ring-1 aria-[current=page]:ring-border",
          )}
        >
          {filter.label}
        </a>
      ))}
    </nav>
  );
}
