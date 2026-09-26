import { Timer } from "lucide-react";
import { useEffect, useRef } from "react";

import { AnswerBody } from "@/chat/AnswerBody";
import { EvidencePanel } from "@/chat/evidence/EvidencePanel";
import { words } from "@/chat/evidence/model";
import { Feedback } from "@/chat/Feedback";
import { Notice } from "@/chat/Notice";
import { progressText, type AnswerBlock, type Turn } from "@/chat/turn";
import { fmtMs } from "@/lib/format";
import type { Me } from "@/lib/types";

function Progress({ turn }: { turn: Turn }) {
  return (
    <div className="progress flex flex-wrap items-center gap-x-3 gap-y-2 text-sm text-subtle" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{progressText(turn)}</span>
      {turn.stages.length > 0 && (
        <ol className="flex flex-wrap gap-1.5" aria-label="Steps finished">
          {turn.stages.map((stage, index) => (
            <li
              key={index}
              className="rounded-md border border-border bg-surface-sunken px-2 py-px font-mono text-xs text-muted"
            >
              {words(stage.name)} {fmtMs(stage.ms)}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function Footer({ turn }: { turn: Turn }) {
  if (!turn.done) return null;
  const total = turn.done.total_ms ?? (turn.finishedAt ?? turn.startedAt) - turn.startedAt;
  const requestId = turn.done.request_id;
  const route = turn.done.route;
  return (
    <div className="footer mt-5 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-border pt-4 text-xs text-subtle">
      <span className="inline-flex items-center gap-1.5">
        <Timer className="size-3.5" aria-hidden="true" />
        Finished in {fmtMs(total)}
      </span>
      <span>
        Request <code>{requestId.slice(0, 8)}</code>
      </span>
      {route && (
        <span>
          Route <code>{route}</code>
        </span>
      )}
      <Feedback requestId={requestId} />
    </div>
  );
}

/** One question and its reply: the answer as it streams, then its evidence and a footer once it is done. */
export function TurnView({ turn, me, onFill }: { turn: Turn; me: Me; onFill: (text: string) => void }) {
  const article = useRef<HTMLElement>(null);
  useEffect(() => {
    article.current?.scrollIntoView({ block: "start" });
  }, []);
  const answer = turn.blocks.find((block): block is AnswerBlock => block.type === "answer");
  return (
    <article ref={article} className="turn mb-10">
      <p className="question mb-3 ml-auto w-fit max-w-[85%] rounded-2xl rounded-br-md bg-bubble px-4 py-2.5 whitespace-pre-wrap text-foreground">
        {turn.question}
      </p>
      <section className="answer rounded-xl border border-border bg-surface p-5 shadow-xs sm:p-6" aria-label="Reply">
        {!turn.finished && <Progress turn={turn} />}
        {turn.blocks.map((block, index) =>
          block.type === "notice" ? (
            <Notice key={index} notice={block} onFill={onFill} />
          ) : (
            <AnswerBody key={index} block={block} turnId={turn.id} />
          ),
        )}
        {turn.finished && (
          <EvidencePanel
            evidence={turn.evidence}
            stages={turn.stages}
            verifier={turn.verifier}
            me={me}
            numbers={answer?.numbers ?? new Map<string, number>()}
          />
        )}
        {turn.finished && <Footer turn={turn} />}
      </section>
    </article>
  );
}
