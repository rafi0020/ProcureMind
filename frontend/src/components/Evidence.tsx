"use client";

import { useApi } from "@/lib/api";
import { cell } from "@/lib/format";
import type { Evidence } from "@/lib/types";
import { Chart } from "./Chart";
import { Drawer, ErrorState, Loading } from "./ui";
import type { ChartSpec } from "@/lib/types";

/** The stored result behind a figure: the SQL that ran and the rows it returned, or a calculation's output. */
export function EvidenceDrawer({ runId, handle, onClose }: { runId: string; handle: string; onClose: () => void }) {
  const { data, error, isLoading } = useApi<Evidence>(`/api/investigations/${runId}/evidence/${handle}`);
  const p = data?.payload;
  const values = p?.values as Record<string, number | null> | undefined;
  const notes = (p?.notes as string[] | undefined) ?? [];
  return (
    <Drawer title={`Evidence ${handle}`} onClose={onClose}>
      {isLoading && <Loading />}
      {error && <ErrorState error={error} />}
      {data && p && (
        <>
          <div className="flex flex-wrap gap-2">
            <span className="chip chip--matched">{data.kind === "query" ? "Query result" : data.kind === "compute" ? `Calculation: ${String(p.op)}` : "Chart"}</span>
            {data.kind !== "chart" && <span className="chip measured">{data.row_count} rows</span>}
            {p.truncated === true && <span className="chip chip--open">capped at 5,000 rows</span>}
          </div>
          {typeof p.purpose === "string" && <p className="ink-2">{p.purpose}</p>}
          {data.sql && (
            <div>
              <div className="label mb-1">SQL that ran (read-only role, this organisation only)</div>
              <div className="well code">{data.sql}</div>
            </div>
          )}
          {typeof p.source === "string" && <p className="label">Calculated from <span className="measured">{p.source}</span></p>}
          {Array.isArray(p.source) && <p className="label">Calculated from <span className="measured">{p.source.join(", ")}</span></p>}
          {values && Object.keys(values).length > 0 && (
            <table className="table">
              <tbody>
                {Object.entries(values).map(([k, v]) => (<tr key={k}><td>{k.replace(/_/g, " ")}</td><td className="num">{cell(v)}</td></tr>))}
              </tbody>
            </table>
          )}
          {notes.map((n) => (<p key={n} className="muted">{n}</p>))}
          {data.kind === "chart" && <Chart spec={p as unknown as ChartSpec} />}
          {data.kind !== "chart" && p.columns && p.rows && (
            <div className="panel table-wrap">
              <table className="table">
                <thead><tr>{p.columns.map((c) => (<th key={c}>{c}</th>))}</tr></thead>
                <tbody>
                  {p.rows.slice(0, 100).map((row, i) => (
                    <tr key={i}>{row.map((v, j) => (<td key={j} className={typeof v === "number" ? "num" : ""}>{cell(v)}</td>))}</tr>
                  ))}
                </tbody>
              </table>
              {p.rows.length === 0 && <p className="muted p-4">This query returned no rows.</p>}
              {p.rows.length > 100 && <p className="label p-3">Showing the first 100 of {data.row_count} rows.</p>}
            </div>
          )}
        </>
      )}
    </Drawer>
  );
}
