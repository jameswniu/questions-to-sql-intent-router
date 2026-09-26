import type { EvalTable } from "@/dashboard/types";

/** The latest eval run for each split, one column per run and one row per metric. */
export function EvalRuns({ evals }: { evals: EvalTable }) {
  return (
    <div className="table-wrap w-full">
      <table className="metrics">
        <thead>
          <tr>
            <th scope="col">Metric</th>
            {evals.runs.map((run) => (
              <th key={`${run.split}-${run.date}`} scope="col" className="text-right">
                {run.split}
                <span className="sub">
                  {run.date}, {run.mode}
                  {run.commit && (
                    <>
                      , <code>{run.commit.slice(0, 7)}</code>
                    </>
                  )}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {evals.rows.map((row) => (
            <tr key={row.metric}>
              <th scope="row">
                <code>{row.metric}</code>
              </th>
              {row.values.map((value, index) => (
                <td key={index} className="num">
                  {value}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
