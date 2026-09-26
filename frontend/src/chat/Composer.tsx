import { ArrowUp, Square } from "lucide-react";
import { useEffect, useLayoutEffect, useRef } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  onAsk: (question: string) => void;
  onStop: () => void;
  busy: boolean;
  /** Changes when the page fills the box, such as from an example or an option, so the box takes the focus. */
  focusKey: number;
}

/**
 * The question box, fixed under the thread. Enter sends and Shift+Enter starts a new line. While an answer streams
 * the button stops it instead.
 */
export function Composer({ value, onChange, onAsk, onStop, busy, focusKey }: ComposerProps) {
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (focusKey) box.current?.focus();
  }, [focusKey]);
  // The box grows with the question, up to 40% of the window, and scrolls past that.
  useLayoutEffect(() => {
    const field = box.current;
    if (!field) return;
    field.style.height = "auto";
    field.style.height = `${Math.min(field.scrollHeight, Math.round(window.innerHeight * 0.4))}px`;
  }, [value]);

  return (
    <form
      id="composer"
      className="composer fixed inset-x-0 bottom-0 z-20 px-4 pt-6 pb-4 sm:px-6"
      autoComplete="off"
      onSubmit={(event) => {
        event.preventDefault();
        if (busy) {
          onStop();
          return;
        }
        const question = value.trim();
        if (!question) return;
        onChange("");
        onAsk(question);
      }}
    >
      <div className="composer-inner mx-auto flex max-w-[768px] items-end gap-2 rounded-2xl border border-control bg-surface p-2 pl-4 shadow-md transition-[border-color,box-shadow] focus-within:border-accent focus-within:ring-4 focus-within:ring-accent/15">
        <label htmlFor="q" className="sr-only">
          Your question
        </label>
        <textarea
          ref={box}
          id="q"
          name="q"
          rows={2}
          maxLength={1000}
          value={value}
          placeholder="Ask about a claim, a figure, or what a policy says"
          onChange={(event) => {
            onChange(event.currentTarget.value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          className="min-h-[3.2rem] flex-1 resize-none bg-transparent py-2 text-base leading-normal text-foreground outline-none placeholder:text-subtle"
        />
        <Button
          id="send"
          type="submit"
          size="lg"
          variant={busy ? "outline" : "primary"}
          className={cn("rounded-xl px-4", busy && "stop")}
        >
          {busy ? <Square className="size-3.5 fill-current" aria-hidden="true" /> : <ArrowUp aria-hidden="true" />}
          {busy ? "Stop" : "Send"}
        </Button>
      </div>
    </form>
  );
}
