import { ArrowRight, BookOpenText, ChartColumn, TrendingUp, type LucideIcon } from "lucide-react";

import type { Me } from "@/lib/types";

const EXAMPLES: { icon: LucideIcon; kind: string; question: string }[] = [
  { icon: ChartColumn, kind: "A figure", question: "How much did we pay on hail claims in Colorado in Q2 2025?" },
  {
    icon: BookOpenText,
    kind: "Policy wording",
    question: "What does the HO-2025 policy say about the wind and hail deductible?",
  },
  { icon: TrendingUp, kind: "What drove a change", question: "Why were paid losses in the West so high in Q2 2025?" },
];

/** The empty thread: who the questions run as, and three questions to start from. */
export function Intro({ me, onPick }: { me: Me; onPick: (question: string) => void }) {
  return (
    <section id="intro" className="intro pt-4 sm:pt-10">
      <h1 className="text-2xl font-semibold tracking-tight text-balance sm:text-3xl">
        Ask about claims, payments and policy wording
      </h1>
      <p className="mt-4 text-muted">
        Questions run as <strong className="font-semibold text-foreground">{me.name}</strong> on the database login{" "}
        <code className="rounded-md border border-border bg-surface-sunken px-1.5 py-px text-foreground">
          {me.db_role}
        </code>
        . Row-level security decides which rows come back, so an answer only uses what this user may see.
      </p>
      <p className="mt-8 mb-3 text-sm font-semibold text-subtle">Try one of these, or write your own:</p>
      <ul className="examples grid gap-3">
        {EXAMPLES.map(({ icon: Icon, kind, question }) => (
          <li key={question}>
            <button
              type="button"
              data-fill={question}
              onClick={() => {
                onPick(question);
              }}
              className="group flex w-full cursor-pointer items-center gap-4 rounded-xl border border-border bg-surface px-4 py-3.5 text-left shadow-xs transition-colors hover:border-accent-border hover:bg-accent-soft"
            >
              <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent-text transition-colors group-hover:bg-surface">
                <Icon className="size-5" aria-hidden="true" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-xs font-medium text-subtle">{kind}</span>
                <span className="block text-sm font-medium text-foreground">{question}</span>
              </span>
              <ArrowRight
                className="size-4 shrink-0 text-subtle transition-transform group-hover:translate-x-0.5 group-hover:text-accent-text"
                aria-hidden="true"
              />
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
