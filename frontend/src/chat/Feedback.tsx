import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Hint } from "@/components/ui/tooltip";
import { sendFeedback } from "@/lib/api";
import { cn } from "@/lib/cn";

/**
 * The thumbs under an answer. Only the user who asked can rate it, which the server checks. A thumbs down asks what
 * was wrong, and the note is redacted the same way a question is.
 */
export function Feedback({ requestId }: { requestId: string }) {
  const [rating, setRating] = useState<1 | -1 | null>(null);
  const [status, setStatus] = useState("Useful?");
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");

  const rate = async (value: 1 | -1) => {
    setRating(value);
    const saved = await sendFeedback(requestId, value);
    setStatus(saved ? "Thanks, noted." : "Could not save that.");
    if (saved && value < 0) setAsking(true);
  };

  const thumbs: [1 | -1, string, typeof ThumbsUp][] = [
    [1, "Useful", ThumbsUp],
    [-1, "Not useful", ThumbsDown],
  ];

  return (
    <>
      <div className="feedback ml-auto flex items-center gap-1.5">
        <span role="status" className="mr-1">
          {status}
        </span>
        {thumbs.map(([value, label, Icon]) => (
          <Hint key={label} label={label}>
            <button
              type="button"
              aria-label={label}
              aria-pressed={rating === value}
              disabled={rating !== null}
              onClick={() => void rate(value)}
              className={cn(
                "thumb grid size-8 cursor-pointer place-items-center rounded-lg border border-border-strong bg-surface text-muted transition-colors",
                "hover:border-accent hover:bg-accent-soft hover:text-accent-text disabled:cursor-default disabled:hover:border-border-strong disabled:hover:bg-surface disabled:hover:text-muted",
                "aria-pressed:border-accent aria-pressed:bg-accent aria-pressed:text-on-accent disabled:aria-pressed:border-accent disabled:aria-pressed:bg-accent disabled:aria-pressed:text-on-accent",
                value < 0 && "down",
              )}
            >
              <Icon className="size-4" aria-hidden="true" />
            </button>
          </Hint>
        ))}
      </div>
      {asking && (
        <form
          className="why-not flex w-full gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            const text = note.trim();
            if (!text) return;
            setAsking(false);
            void sendFeedback(requestId, -1, text).then((saved) => {
              setStatus(saved ? "Thanks, noted." : "Could not save the note.");
            });
          }}
        >
          <input
            type="text"
            maxLength={1000}
            value={note}
            onChange={(event) => {
              setNote(event.currentTarget.value);
            }}
            placeholder="What was wrong? (optional)"
            aria-label="What was wrong"
            className="h-9 min-w-0 flex-1 rounded-lg border border-control bg-surface px-3 text-sm text-foreground placeholder:text-subtle"
          />
          <Button type="submit" variant="outline" size="md">
            Send
          </Button>
        </form>
      )}
    </>
  );
}
