import * as TabsPrimitive from "@radix-ui/react-tabs";
import { ChevronRight } from "lucide-react";
import { useState } from "react";

import { Checks } from "@/chat/evidence/Checks";
import { cellText, evidenceTabs, rowsOf, scanFields, type EvidenceTab } from "@/chat/evidence/model";
import { Passages } from "@/chat/evidence/Passages";
import { RowsTable } from "@/chat/evidence/RowsTable";
import { ScanFigure } from "@/chat/evidence/ScanFigure";
import { SandboxBlock } from "@/chat/evidence/SplitTables";
import { SqlBlock } from "@/chat/evidence/SqlBlock";
import { Timings } from "@/chat/evidence/Timings";
import type { Verifier } from "@/chat/turn";
import { TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { EvidenceEvent, Me, StageEvent } from "@/lib/types";

interface EvidenceProps {
  evidence: EvidenceEvent[];
  stages: StageEvent[];
  verifier: Verifier | null;
  me: Me;
  /** The number each cited source goes by, so a cited passage shows it. */
  numbers: Map<string, number>;
}

function TabBody({ tab, props, rows }: { tab: EvidenceTab; props: EvidenceProps; rows: unknown[] }) {
  switch (tab.kind) {
    case "sql":
      return (
        <div className="space-y-5">
          {tab.items.map((item, index) => (
            <SqlBlock key={index} payload={item.payload} me={props.me} />
          ))}
        </div>
      );
    case "rows":
      return tab.items.map((item, index) => <RowsTable key={index} payload={item.payload} />);
    case "chunks":
      return tab.items.map((item, index) => <Passages key={index} payload={item.payload} numbers={props.numbers} />);
    case "scan":
      return (
        <div className="grid gap-3 sm:grid-cols-2">
          {tab.items
            .flatMap((item) => scanFields(item.payload))
            .map((field, index) => (
              <ScanFigure key={index} field={field} />
            ))}
        </div>
      );
    case "sandbox":
      return tab.items.map((item, index) => <SandboxBlock key={index} payload={item.payload} rows={rows} />);
    case "checks":
      return props.verifier && <Checks verifier={props.verifier} />;
    case "timings":
      return <Timings stages={props.stages} />;
    case "other":
      return tab.items.map((item, index) => (
        <pre key={index} className="code">
          <code>{cellText(item.payload)}</code>
        </pre>
      ));
  }
}

/**
 * The evidence under an answer, folded until opened: its summary line says what there is, and each kind has a tab.
 * Every panel stays in the page, hidden until its tab is chosen, so the evidence can be searched and checked whole.
 */
export function EvidencePanel(props: EvidenceProps) {
  const { evidence, stages, verifier } = props;
  const tabs = evidenceTabs(evidence, stages, verifier);
  const [chosen, setChosen] = useState(() => tabs[0]?.id ?? "");
  if (!tabs.length) return null;
  const rows = evidence.filter((item) => item.kind === "rows").flatMap((item) => rowsOf(item.payload));
  return (
    <TabsPrimitive.Root asChild value={chosen} onValueChange={setChosen}>
      <details className="evidence group/evidence mt-5 rounded-xl border border-border bg-surface">
        <summary className="flex cursor-pointer items-center gap-3 rounded-xl px-4 py-3 text-sm font-medium text-muted transition-colors select-none group-open/evidence:rounded-b-none hover:bg-surface-hover hover:text-foreground">
          <ChevronRight className="chevron size-4 shrink-0 transition-transform" aria-hidden="true" />
          <span className="text-foreground">Evidence</span>
          <span className="flex min-w-0 flex-wrap gap-1.5">
            {tabs.map((tab) => (
              <span key={tab.id} className="chip">
                {tab.chip}
              </span>
            ))}
          </span>
        </summary>
        <TabsList aria-label="Evidence" className="border-t px-2">
          {tabs.map((tab) => (
            <TabsTrigger key={tab.id} value={tab.id} className="evidence-tab">
              {tab.label}
              {tab.count !== null && <span className="count">{tab.count}</span>}
            </TabsTrigger>
          ))}
        </TabsList>
        {tabs.map((tab) => (
          <TabsContent key={tab.id} value={tab.id} forceMount hidden={chosen !== tab.id} asChild>
            <section className="evidence-panel min-w-0 p-4 outline-none sm:p-5">
              <h3 className="sr-only">{tab.heading}</h3>
              <TabBody tab={tab} props={props} rows={rows} />
            </section>
          </TabsContent>
        ))}
      </details>
    </TabsPrimitive.Root>
  );
}
