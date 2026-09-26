import { useState } from "react";

import { cellText, columnsOf, numeric, record, rowsOf } from "@/chat/evidence/model";

const SHOWN = 20;

/**
 * The rows a query returned, with every column any row has. Past the first twenty the rest fold away, since they are
 * often another query's and hold the only values in some columns, so they can be shown.
 */
export function RowsTable({ payload }: { payload: unknown }) {
  const [all, setAll] = useState(false);
  const rows = rowsOf(payload);
  if (!rows.length) return <p className="legend">No rows came back.</p>;
  const fields = record(payload);
  const columns = Array.isArray(fields.columns) ? fields.columns.map(cellText) : columnsOf(rows);
  const truncated = Boolean(fields.truncated);
  const counted = Number(fields.row_count);
  const total = Number.isFinite(counted) && counted > 0 ? counted : rows.length;
  const folded = rows.length > SHOWN && !all;
  const legend = (shown: number) => `Showing ${shown} of ${total}${truncated ? " or more" : ""} rows.`;
  return (
    <>
      <div className="table-wrap">
        <table className="rows">
          <thead>
            <tr>
              {columns.map((name) => (
                <th key={name} scope="col">
                  {name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index} hidden={folded && index >= SHOWN}>
                {columns.map((name, at) => {
                  // A row without one of the columns leaves that cell blank.
                  const value = Array.isArray(row) ? (row as unknown[])[at] : record(row)[name];
                  return (
                    <td key={name} className={numeric(value) ? "num" : undefined}>
                      {cellText(value)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(total > SHOWN || truncated) && (
        <p className="legend">
          {legend(folded ? SHOWN : rows.length)}
          {folded && (
            <>
              {" "}
              <button
                type="button"
                className="more"
                onClick={() => {
                  setAll(true);
                }}
              >
                Show all
              </button>
            </>
          )}
        </p>
      )}
    </>
  );
}
