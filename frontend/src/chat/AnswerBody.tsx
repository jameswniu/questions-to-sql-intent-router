import { TriangleAlert } from "lucide-react";
import { Fragment } from "react";

import type { Piece } from "@/chat/answer";
import type { AnswerBlock } from "@/chat/turn";
import { sourceLabel } from "@/lib/sources";

function Marked({ pieces, turnId }: { pieces: Piece[]; turnId: number }) {
  return pieces.map((piece, index) =>
    typeof piece === "string" ? (
      <Fragment key={index}>{piece}</Fragment>
    ) : (
      <sup key={index} className="cite">
        <a href={`#cite-${turnId}-${piece}`} aria-label={`Source ${piece}`}>
          {piece}
        </a>
      </sup>
    ),
  );
}

/**
 * An answer as the server wrote it: its paragraphs and bullets, a numbered marker after each claim that cites a
 * source, what the verifier could not confirm, and the numbered sources the markers point at.
 */
export function AnswerBody({ block, turnId }: { block: AnswerBlock; turnId: number }) {
  return (
    <>
      <div className="answer-text">
        {block.paragraphs.map((paragraph, index) =>
          paragraph.kind === "p" ? (
            <p key={index}>
              <Marked pieces={paragraph.pieces} turnId={turnId} />
            </p>
          ) : (
            <ul key={index}>
              {paragraph.items.map((item, at) => (
                <li key={at}>
                  <Marked pieces={item} turnId={turnId} />
                </li>
              ))}
            </ul>
          ),
        )}
      </div>
      {block.caveats.map((reason, index) => (
        <p
          key={index}
          className="caveat mt-4 flex gap-2.5 rounded-lg border border-warning-border bg-warning-soft px-3.5 py-2.5 text-sm text-foreground"
        >
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
          {reason}
        </p>
      ))}
      {block.citations.length > 0 && (
        <ol className="citations">
          {block.citations.map((source, index) => (
            <li key={index} id={`cite-${turnId}-${index + 1}`}>
              {sourceLabel(source)}
            </li>
          ))}
        </ol>
      )}
    </>
  );
}
