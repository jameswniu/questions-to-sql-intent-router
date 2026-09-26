import { Check, Copy, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { cellText, paramText, placeholders, record } from "@/chat/evidence/model";
import { sqlTokens, type TokenKind } from "@/chat/evidence/sqlTokens";
import { Hint } from "@/components/ui/tooltip";
import { cn } from "@/lib/cn";
import type { Me } from "@/lib/types";

const TOKEN_CLASS: Record<TokenKind, string | undefined> = {
  keyword: "tok-kw",
  function: "tok-fn",
  string: "tok-str",
  number: "tok-num",
  placeholder: "tok-ph",
  comment: "tok-cm",
  plain: undefined,
};

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const label = copied ? "Copied" : "Copy SQL";
  return (
    <Hint label={label}>
      <button
        type="button"
        aria-label={label}
        onClick={() => {
          void navigator.clipboard.writeText(text).then(() => {
            setCopied(true);
            window.setTimeout(() => {
              setCopied(false);
            }, 1500);
          });
        }}
        className="absolute top-2 right-2 grid size-7 cursor-pointer place-items-center rounded-md border border-border bg-surface text-subtle opacity-80 shadow-xs transition hover:text-foreground hover:opacity-100"
      >
        {copied ? <Check className="size-3.5" aria-hidden="true" /> : <Copy className="size-3.5" aria-hidden="true" />}
      </button>
    </Hint>
  );
}

// What ran was parameterized, so the values stay out of the SQL text and are listed under it, in placeholder order.
function BoundParams({ sql, params }: { sql: string; params: unknown }) {
  const values = Array.isArray(params) ? params : [];
  if (!values.length) {
    const slots = placeholders(sql);
    if (!slots) return null;
    const rest = `${slots === 1 ? "placeholder was" : "placeholders were"} bound as parameters; their values aren't shown here.`;
    return (
      <p className="legend">
        The {slots} <code>%s</code> {rest}
      </p>
    );
  }
  return (
    <>
      <p className="legend">
        Parameters bound to the <code>%s</code> placeholders, in order:
      </p>
      <ol className="params">
        {values.map((value, index) => (
          <li key={index}>
            <code>{paramText(value)}</code>
          </li>
        ))}
      </ol>
    </>
  );
}

/** One statement as it ran: the login it ran on, the SQL itself and the values bound to it. */
export function SqlBlock({ payload, me }: { payload: unknown; me: Me }) {
  const fields = record(payload);
  const text = typeof payload === "string" ? payload : cellText(fields.sql);
  if (!text) return null;
  const role = cellText(fields.role) || me.db_role;
  return (
    <div className="sql-block">
      <p className="role-note flex items-start gap-2">
        <ShieldCheck className="mt-0.5 size-4 shrink-0 text-success" aria-hidden="true" />
        <span>
          Ran on {me.name}&apos;s own database login, <code>{role}</code>, so row-level security applied.
        </span>
      </p>
      <div className="relative">
        <pre className="sql">
          <code>
            {sqlTokens(text).map(([kind, token], index) => (
              <span key={index} className={cn(TOKEN_CLASS[kind])}>
                {token}
              </span>
            ))}
          </code>
        </pre>
        <CopyButton text={text} />
      </div>
      <BoundParams sql={text} params={typeof payload === "object" ? fields.params : null} />
    </div>
  );
}
