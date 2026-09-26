import { Check, X } from "lucide-react";

import type { Verifier } from "@/chat/turn";

/**
 * What the verifier did to the draft: each claim it kept, checked against the evidence, and for each claim it cut
 * only the reason. A cut claim's words never reach the browser; its figure was wrong, or its source is not this
 * user's to read.
 */
export function Checks({ verifier }: { verifier: Verifier }) {
  const kept = verifier.kept.length;
  return (
    <>
      <p className="legend">{`${kept} ${kept === 1 ? "claim" : "claims"} kept, ${verifier.cut} cut.`}</p>
      {kept + verifier.reasons.length > 0 && (
        <ul className="claims">
          {verifier.kept.map((claim, index) => (
            <li key={`kept-${index}`}>
              <span className="tag">
                <Check aria-hidden="true" />
                Kept
              </span>
              <span>{typeof claim === "string" ? claim : (claim.text ?? "")}</span>
            </li>
          ))}
          {verifier.reasons.map((reason, index) => (
            <li key={`cut-${index}`} className="cut">
              <span className="tag">
                <X aria-hidden="true" />
                Cut
              </span>
              <span>{reason}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
