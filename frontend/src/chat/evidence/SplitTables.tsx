import { Fragment } from "react";

import {
  SPLIT_COLUMNS,
  SPLIT_NOTE,
  asList,
  cellText,
  dimensionOf,
  isSplit,
  words,
  type SplitLine,
  type SplitRun,
} from "@/chat/evidence/model";
import { cn } from "@/lib/cn";

const percent = new Intl.NumberFormat("en-US", {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

/** A group's share of the whole change as a bar from a centre line: right for a rise, left for a fall. */
function ShareBar({ share }: { share: number }) {
  const size = Math.min(Math.abs(share), 1) * 50;
  return (
    <span
      aria-hidden="true"
      className="relative hidden h-2 w-12 shrink-0 rounded-full bg-surface-hover sm:inline-block"
    >
      <span className="absolute inset-y-0 left-1/2 w-px bg-border-strong" />
      <span
        className={cn("absolute inset-y-0 rounded-full", share >= 0 ? "tone-accent" : "tone-bad")}
        style={share >= 0 ? { left: "50%", width: `${size}%` } : { right: "50%", width: `${size}%` }}
      />
    </span>
  );
}

function SplitTable({ run, dimension }: { run: SplitRun; dimension: string | null }) {
  const groups = Array.isArray(run.groups) ? run.groups : [];
  const whole: SplitLine = { ...run, group: "Total", share: Number(run.delta_total) ? 1 : null };
  const figures = [...groups, whole]
    .flatMap((line) => [line.delta_total, line.count_effect, line.mean_effect])
    .filter((value) => value != null);
  const places = figures.every((value) => Number.isInteger(Number(value))) ? 0 : 2;
  const amount = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: places,
    maximumFractionDigits: places,
    signDisplay: "exceptZero",
  });
  const cell = (value: unknown, format: Intl.NumberFormat) =>
    value == null || !Number.isFinite(Number(value)) ? "" : format.format(Number(value));
  const line = (row: SplitLine, key: string) => {
    const share = Number(row.share);
    return (
      <tr key={key}>
        <th scope="row">{cellText(row.group)}</th>
        <td className="num">{cell(row.delta_total, amount)}</td>
        <td className="num">{cell(row.count_effect, amount)}</td>
        <td className="num">{cell(row.mean_effect, amount)}</td>
        <td className="num">
          <span className="inline-flex items-center justify-end gap-2">
            {row.share != null && Number.isFinite(share) && <ShareBar share={share} />}
            {/* As wide as the widest share, 100.0% in bold, so every row's bar sits in one column. */}
            <span className="min-w-[4em]">{cell(row.share, percent)}</span>
          </span>
        </td>
      </tr>
    );
  };
  return (
    <div className="table-wrap">
      <table className="split">
        {dimension && <caption>By {words(dimension)}</caption>}
        <thead>
          <tr>
            {SPLIT_COLUMNS.map((name) => (
              <th key={name} scope="col">
                {name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {groups.map((group, index) => line(group, String(index)))}
          {!groups.length && line(whole, "total")}
        </tbody>
        {groups.length > 0 && <tfoot>{line(whole, "total")}</tfoot>}
      </table>
    </div>
  );
}

/**
 * What the sandbox worked out. A driver split shows how much of a change came from the number of claims and how much
 * from the average per claim, overall and for each group. Anything else it returned is shown as it came.
 */
export function SandboxBlock({ payload, rows }: { payload: unknown; rows: unknown[] }) {
  const runs = asList(payload);
  return (
    <>
      {runs.some(isSplit) && <p className="legend">{SPLIT_NOTE}</p>}
      {runs.map((run, index) => {
        if (isSplit(run)) return <SplitTable key={index} run={run} dimension={dimensionOf(run, rows)} />;
        if (typeof run !== "object" || run === null) {
          return (
            <pre key={index} className="code">
              <code>{cellText(run)}</code>
            </pre>
          );
        }
        return (
          <Fragment key={index}>
            {Object.entries(run)
              .filter(([, value]) => value != null && value !== "")
              .map(([key, value]) => (
                <Fragment key={key}>
                  <p className="legend">{words(key)}</p>
                  <pre className="code">
                    <code>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</code>
                  </pre>
                </Fragment>
              ))}
          </Fragment>
        );
      })}
    </>
  );
}
