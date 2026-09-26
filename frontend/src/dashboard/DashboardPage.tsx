import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Inbox } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { WhoChip } from "@/components/Identity";
import { ErrorPanel, Skeleton } from "@/components/StatePanels";
import { TopBar } from "@/components/TopBar";
import { ChartFigure } from "@/dashboard/BarChart";
import { EvalRuns } from "@/dashboard/EvalRuns";
import { SourceFilter } from "@/dashboard/SourceFilter";
import { StatTiles } from "@/dashboard/StatTiles";
import type { DashboardView, Filter } from "@/dashboard/types";
import { getDashboard } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useSession } from "@/session";

const SOURCES = ["ui", "eval", "replay"];

function sourceInAddress(): string | null {
  const found = new URLSearchParams(window.location.search).get("source");
  return found && SOURCES.includes(found) ? found : null;
}

function Panel({
  title,
  description,
  aside,
  children,
  className,
}: {
  title: string;
  description?: string;
  aside?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("panel min-w-0 rounded-xl border border-border bg-surface p-5 shadow-xs sm:p-6", className)}>
      <div className="mb-5 flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
          {description && <p className="muted mt-1 text-sm text-muted">{description}</p>}
        </div>
        {aside}
      </div>
      {children}
    </section>
  );
}

function LatencyKey() {
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted" aria-label="Key">
      <li className="flex items-center gap-1.5">
        <span aria-hidden="true" className="tone-accent-light size-2.5 rounded-sm" />
        First event
      </li>
      <li className="flex items-center gap-1.5">
        <span aria-hidden="true" className="tone-accent size-2.5 rounded-sm" />
        Total
      </li>
      <li className="flex items-center gap-1.5">
        <svg aria-hidden="true" width="10" height="12" className="stroke-muted">
          <line x1="5" x2="5" y1="0" y2="12" strokeWidth="1.5" strokeDasharray="3 2" />
        </svg>
        p95 budget
      </li>
    </ul>
  );
}

function Populated({ data }: { data: DashboardView }) {
  return (
    <>
      <StatTiles tiles={data.tiles} />
      <Panel
        title="Latency by route"
        description="Time to the first streamed event and to the end of the answer. The dashed line is the route's p95 budget."
        aside={<LatencyKey />}
      >
        {data.latency.length ? (
          <div className="grid gap-x-10 gap-y-8 md:grid-cols-2">
            {data.latency.map((chart) => (
              <ChartFigure key={chart.title} chart={chart} />
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted">No finished requests yet.</p>
        )}
      </Panel>
      <Panel title="Traffic and outcomes">
        <div className="grid gap-x-10 gap-y-8 md:grid-cols-2">
          {data.by_route && <ChartFigure chart={data.by_route} />}
          {data.outcomes && <ChartFigure chart={data.outcomes} />}
        </div>
      </Panel>
      <div className="grid gap-4 lg:grid-cols-3">
        <Panel title="Verifier" description={data.verifier.note}>
          {data.verifier.chart && <ChartFigure chart={data.verifier.chart} />}
        </Panel>
        <Panel title="Feedback">
          {data.feedback ? (
            <ChartFigure chart={data.feedback} />
          ) : (
            <p className="muted text-sm text-muted">No ratings yet.</p>
          )}
        </Panel>
        <Panel title="Cost per question" description={data.cost.note}>
          {data.cost.chart && <ChartFigure chart={data.cost.chart} />}
        </Panel>
      </div>
    </>
  );
}

function Loading() {
  return (
    <div className="mt-6 space-y-4" aria-hidden="true">
      <Skeleton className="h-5 w-2/3" />
      <Skeleton className="h-9 w-72" />
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {[0, 1, 2, 3].map((index) => (
          <Skeleton key={index} className="h-28 rounded-xl" />
        ))}
      </div>
      <Skeleton className="h-72 rounded-xl" />
    </div>
  );
}

/**
 * The operator dashboard: how many requests came in, how fast and how they ended, what the verifier cut, how answers
 * were rated, and the latest eval runs, over the last 30 days. The server decides who may see it.
 */
export default function DashboardPage() {
  const session = useSession();
  const [source, setSource] = useState(sourceInAddress);
  useEffect(() => {
    const back = () => {
      setSource(sourceInAddress());
    };
    window.addEventListener("popstate", back);
    return () => {
      window.removeEventListener("popstate", back);
    };
  }, []);
  const view = useQuery({
    queryKey: ["dashboard", source],
    queryFn: ({ signal }) => getDashboard<DashboardView>(source, signal),
    placeholderData: keepPreviousData,
  });
  const data = view.data;
  const choose = (filter: Filter) => {
    if (filter.source === source) return;
    window.history.pushState(null, "", filter.href);
    setSource(filter.source);
  };

  return (
    <>
      <TopBar page="dashboard" ops identity={session.data && <WhoChip session={session.data} />} />
      <main
        className="dashboard mx-auto w-full max-w-[1200px] px-4 pt-8 pb-16 sm:px-6"
        data-source={data ? (data.source ?? "all") : undefined}
        aria-busy={view.isFetching}
      >
        <h1 className="text-2xl font-semibold tracking-tight">Service dashboard</h1>
        {!data ? (
          view.isError ? (
            <ErrorPanel error={view.error} title="The dashboard could not load" />
          ) : (
            <Loading />
          )
        ) : (
          <>
            <p className="muted mt-1 text-sm text-muted">
              {data.scope} Read from <code>ops.request_log</code>, <code>ops.feedback</code> and{" "}
              <code>ops.eval_runs</code>.
            </p>
            <div className="mt-6">
              <SourceFilter filters={data.filters} current={source} onChoose={choose} />
            </div>
            <div className={cn("mt-6 space-y-4 transition-opacity", view.isPlaceholderData && "opacity-60")}>
              {data.requests ? (
                <Populated data={data} />
              ) : (
                <section className="panel empty rounded-xl border border-dashed border-border-strong px-6 py-10 text-center">
                  <Inbox className="mx-auto size-6 text-subtle" aria-hidden="true" />
                  <h2 className="mt-3 text-lg font-semibold">No requests yet</h2>
                  <p className="mt-1 text-sm text-muted">
                    Ask a question in the chat and it shows up here within a second.
                  </p>
                </section>
              )}
              <Panel title="Latest eval runs">
                {data.evals ? (
                  <EvalRuns evals={data.evals} />
                ) : (
                  <p className="muted text-sm text-muted">No eval runs yet.</p>
                )}
              </Panel>
            </div>
          </>
        )}
      </main>
    </>
  );
}
