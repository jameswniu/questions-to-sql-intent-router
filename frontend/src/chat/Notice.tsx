import { Ban, CalendarRange, CircleAlert, MessageCircleQuestion, type LucideIcon } from "lucide-react";

import type { NoticeBlock, NoticeKind } from "@/chat/turn";
import { cn } from "@/lib/cn";

const LOOK: Record<NoticeKind, { icon: LucideIcon; box: string; label: string }> = {
  refused: { icon: Ban, box: "border-danger-border bg-danger-soft", label: "text-danger" },
  error: { icon: CircleAlert, box: "border-danger-border bg-danger-soft", label: "text-danger" },
  outside: { icon: CalendarRange, box: "border-warning-border bg-warning-soft", label: "text-warning" },
  clarify: { icon: MessageCircleQuestion, box: "border-accent-border bg-accent-soft", label: "text-accent-text" },
};

/**
 * A reply that is not an answer: refused at the gate, outside the data, asking for one more detail, or an error. A
 * question asked back offers its answers as buttons that fill in the question box.
 */
export function Notice({ notice, onFill }: { notice: NoticeBlock; onFill: (text: string) => void }) {
  const look = LOOK[notice.kind];
  const Icon = look.icon;
  return (
    <div className={cn("notice", notice.kind, "mb-4 rounded-lg border p-4 last:mb-0", look.box)}>
      <span className={cn("label flex items-center gap-2 text-sm font-semibold", look.label)}>
        <Icon className="size-4 shrink-0" aria-hidden="true" />
        {notice.label}
      </span>
      <p className="mt-1.5 text-foreground">{notice.message}</p>
      {notice.extra && <p className="muted mt-1 text-sm text-muted">{notice.extra}</p>}
      {notice.options.length > 0 && (
        <ul className="options mt-3 flex flex-wrap gap-2">
          {notice.options.map((option) => (
            <li key={option.fill}>
              <button
                type="button"
                data-fill={option.fill}
                onClick={() => {
                  onFill(option.fill);
                }}
                className="cursor-pointer rounded-full border border-accent-border bg-surface px-3.5 py-1 text-sm font-medium text-accent-text shadow-xs transition-colors hover:border-accent hover:bg-accent hover:text-on-accent"
              >
                {option.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
