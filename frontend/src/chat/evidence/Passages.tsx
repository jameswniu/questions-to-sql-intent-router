import { useState } from "react";

import { cellText, passages } from "@/chat/evidence/model";
import { sourceKey, sourceLabel } from "@/lib/sources";

const LONG = 320;

function Passage({ body }: { body: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <p className={open ? "body mt-2 whitespace-pre-line" : "body mt-2 line-clamp-4"}>{body}</p>
      {body.length > LONG && (
        <button
          type="button"
          className="more mt-1"
          aria-expanded={open}
          onClick={() => {
            setOpen(!open);
          }}
        >
          {open ? "Show less" : "Show the whole passage"}
        </button>
      )}
    </>
  );
}

/** The passages retrieval found, one per chunk. A passage the answer cites carries the number its marker shows. */
export function Passages({ payload, numbers }: { payload: unknown; numbers: Map<string, number> }) {
  const chunks = passages(payload);
  return (
    <ul className="chunks">
      {chunks.map((chunk, index) => {
        const score = chunk.score ?? chunk.rerank_score ?? chunk.similarity;
        const body = cellText(chunk.body ?? chunk.text ?? chunk.snippet);
        const keyed = chunk.chunk_id ?? chunk.doc_id;
        const cited = keyed ? numbers.get(sourceKey(chunk, index)) : undefined;
        return (
          <li key={sourceKey(chunk, index)}>
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
              <span className="where">{sourceLabel(chunk)}</span>
              {typeof score === "number" && <span className="score">score {score.toFixed(3)}</span>}
              {cited !== undefined && <span className="cited">Source {cited}</span>}
            </div>
            {body && <Passage body={body} />}
          </li>
        );
      })}
    </ul>
  );
}
