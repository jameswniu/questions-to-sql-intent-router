import { useCallback, useRef, useState } from "react";

import { applyEvent, closes, finish, newTurn, withNotice, type Turn } from "@/chat/turn";
import { askQuestion } from "@/lib/api";
import { readEvents } from "@/lib/sse";

/** The thread: every question asked on this page, its answer as it streams, and a way to stop the one in flight. */
export function useChat() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef<AbortController | null>(null);
  const asked = useRef(0);

  const ask = useCallback(async (question: string) => {
    const id = ++asked.current;
    const update = (change: (turn: Turn) => Turn) => {
      setTurns((all) => all.map((turn) => (turn.id === id ? change(turn) : turn)));
    };
    setTurns((all) => [...all, newTurn(id, question, performance.now())]);
    const controller = new AbortController();
    inFlight.current = controller;
    setBusy(true);
    try {
      const response = await askQuestion(question, controller.signal);
      if (!response.ok || !response.body) {
        const said = (await response.text()) || `The server answered ${response.status}.`;
        update((turn) => ({ ...withNotice(turn, "error", "Not sent", said), ended: true }));
        return;
      }
      let ended = false;
      for await (const [type, data] of readEvents(response.body)) {
        ended ||= closes(type);
        update((turn) => applyEvent(turn, type, data));
      }
      if (!ended) update((turn) => withNotice(turn, "error", "Stopped early", "The answer ended before it finished."));
    } catch (error) {
      if (controller.signal.aborted || (error instanceof DOMException && error.name === "AbortError")) {
        update((turn) => withNotice(turn, "outside", "Stopped", "You stopped this answer."));
      } else {
        update((turn) =>
          withNotice(turn, "error", "Connection lost", "The connection dropped before the answer finished."),
        );
      }
    } finally {
      inFlight.current = null;
      setBusy(false);
      update((turn) => finish(turn, performance.now()));
    }
  }, []);

  const stop = useCallback(() => {
    inFlight.current?.abort();
  }, []);

  return { turns, busy, ask, stop };
}
