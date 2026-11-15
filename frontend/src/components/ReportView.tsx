"use client";

import { Fragment, type ReactNode } from "react";
import type { NumberRef, Report } from "@/lib/types";
import { Chart } from "./Chart";

/** Turn each supported figure in the text into a control that opens the evidence it came from. */
function linkFigures(text: string, refs: NumberRef[], open: (handle: string) => void): ReactNode[] {
  const out: ReactNode[] = [];
  let cursor = 0;
  refs.forEach((ref, i) => {
    const at = text.indexOf(ref.text, cursor);
    if (at < 0) return;
    out.push(<Fragment key={`t${i}`}>{text.slice(cursor, at)}</Fragment>);
    out.push(<button key={`f${i}`} className="fig" title={`Open evidence ${ref.handle}`} onClick={() => open(ref.handle)}>{ref.text}</button>);
    cursor = at + ref.text.length;
  });
  out.push(<Fragment key="rest">{text.slice(cursor)}</Fragment>);
  return out;
}

export function ReportView({ report, onEvidence }: { report: Report; onEvidence: (handle: string) => void }) {
  return (
    <div className="grid gap-6">
      <section className="grid gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2>{report.outcome === "cannot_answer" ? "The agent could not answer this" : "Answer"}</h2>
          {report.outcome === "answered" && report.unsupported.length === 0 && <span className="chip chip--ok">every figure traced</span>}
        </div>
        <p className="prose">{report.summary ? linkFigures(report.summary, report.summary_numbers, onEvidence) : "No supported statement remained in the summary."}</p>
      </section>

      {report.unsupported.length > 0 && (
        <section className="banner banner--bad" aria-label="Removed statements">
          <div className="grid gap-1">
            <strong>{report.unsupported.length} statement{report.unsupported.length > 1 ? "s were" : " was"} removed</strong>
            <span>The agent wrote {report.unsupported.length > 1 ? "these" : "this"}, but a figure in {report.unsupported.length > 1 ? "them" : "it"} could not be found in the evidence:</span>
            <ul className="m-0 pl-5">{report.unsupported.map((s) => (<li key={s}>{s}</li>))}</ul>
          </div>
        </section>
      )}

      {report.findings.length > 0 && (
        <section className="grid gap-2">
          <h2>Findings</h2>
          <div className="findings">
            {report.findings.map((f) => (
              <div key={f.number} className="grid gap-2">
                <p className="prose">{linkFigures(f.claim, f.numbers, onEvidence)}</p>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="label">Based on</span>
                  {f.evidence.map((h) => (<button key={h} className="fig" onClick={() => onEvidence(h)}>{h}</button>))}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {report.charts.map((chart) => (
        <section key={chart.chart_handle ?? chart.title} className="panel">
          <div className="panel__head"><h3>{chart.title}</h3>{chart.handle && <button className="fig" onClick={() => onEvidence(chart.handle!)}>{chart.handle}</button>}</div>
          <div className="panel__body"><Chart spec={chart} /></div>
        </section>
      ))}

      {report.recommendations.length > 0 && (
        <section className="grid gap-2">
          <h2>Recommendations</h2>
          <ul className="prose m-0 pl-5">{report.recommendations.map((r) => (<li key={r}>{r}</li>))}</ul>
          <p className="label">Recommendations are the agent&apos;s suggestions. They are not checked against the evidence.</p>
        </section>
      )}

      {report.evidence.length > 0 && (
        <section className="grid gap-2">
          <h2>Evidence</h2>
          <div className="panel table-wrap">
            <table className="table">
              <thead><tr><th>Handle</th><th>Kind</th><th>What it is</th><th className="num">Rows</th></tr></thead>
              <tbody>
                {report.evidence.map((e) => (
                  <tr key={e.handle}>
                    <td><button className="fig" onClick={() => onEvidence(e.handle)}>{e.handle}</button></td>
                    <td>{e.kind}</td>
                    <td className="ink-2">{e.purpose ?? ""}</td>
                    <td className="num">{e.kind === "chart" ? "" : e.row_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
