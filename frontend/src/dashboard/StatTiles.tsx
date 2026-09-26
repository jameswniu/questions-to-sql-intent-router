import { Activity, CircleCheck, ThumbsUp, Timer, type LucideIcon } from "lucide-react";

import type { Tile } from "@/dashboard/types";
import { cn } from "@/lib/cn";

const ICONS: Record<string, LucideIcon> = {
  Requests: Activity,
  Answered: CircleCheck,
  "Median first event": Timer,
  "Rated useful": ThumbsUp,
};

/** The dashboard's headline numbers, one tile each, worded as the server sends them. */
export function StatTiles({ tiles }: { tiles: Tile[] }) {
  return (
    <div className="tiles grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-4">
      {tiles.map((tile) => {
        const Icon = ICONS[tile.label] ?? Activity;
        // A tile with nothing to count yet, such as "No ratings yet", says so quietly instead of as a headline.
        const counted = /\d/.test(tile.value);
        return (
          <div key={tile.label} className="tile rounded-xl border border-border bg-surface p-4 shadow-xs lg:p-5">
            <div className="flex items-center justify-between gap-2">
              <span className="tile-label text-xs font-medium text-muted">{tile.label}</span>
              <Icon className="size-4 shrink-0 text-subtle" aria-hidden="true" />
            </div>
            <div
              className={cn(
                "tile-value mt-2",
                counted
                  ? "text-2xl font-semibold tracking-tight sm:text-3xl"
                  : "pt-1.5 text-base font-medium text-muted sm:pt-3",
              )}
            >
              {tile.value}
            </div>
          </div>
        );
      })}
    </div>
  );
}
